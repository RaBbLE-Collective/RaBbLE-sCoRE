"""
Object storage: Cloudflare R2 over the S3 API, with request accounting.

R2's free tier is 1M Class A (PUT/COPY/LIST/POST) and 10M Class B (GET/HEAD) requests a
month. Every call counted here lands in db.r2_usage; at R2_CLASS_A_BUDGET / R2_CLASS_B_BUDGET
(Mark's margin under those) all R2 I/O stops and callers keep serving from local disk
until the next UTC month. DELETE is free and uncounted.

Env (all unset = local-only mode: everything works, nothing leaves the box):
  R2_ENDPOINT           https://<account>.r2.cloudflarestorage.com
  R2_ACCESS_KEY_ID      an R2 API token scoped to the one bucket, Object Read & Write
  R2_SECRET_ACCESS_KEY
  R2_BUCKET             a dedicated bucket (never the firmware one)
  R2_PREFIX             key prefix inside it (default "score/")
  R2_CLASS_A_BUDGET     default 800000
  R2_CLASS_B_BUDGET     default 8000000

S3Client is a hand-rolled SigV4 over httpx (already a dependency): no boto3. Anything
with the same five methods can stand in for it (set_client), which is how tests run
without a network.
"""

import hashlib
import hmac
import logging
import os
import threading
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

import httpx

import db

logger = logging.getLogger("rabble.storage")

USAGE_KEY = "accounting/usage.json"
_warned: set[tuple[str, str]] = set()
_lock = threading.Lock()
_client = None            # injected (tests) or built from env
_built_from: tuple | None = None


class Unavailable(Exception):
    """No R2 (local-only mode) or this month's budget is spent: use local disk."""


class PreconditionFailed(RuntimeError):
    """A conditional PUT (If-Match / If-None-Match) was refused: another writer got there first."""


# ── SigV4 ─────────────────────────────────────────────────────────────────────

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign_headers(method: str, host: str, path: str, query: dict, extra: dict, payload_sha: str,
                 access_key: str, secret_key: str, region: str, amz_date: str) -> dict:
    """AWS Signature V4 for the s3 service. path is already URI-encoded. Returns the headers
    to send (host is implied by the URL; the rest including Authorization)."""
    date = amz_date[:8]
    headers = {k.lower(): str(v).strip() for k, v in extra.items()}
    headers.update({"host": host, "x-amz-content-sha256": payload_sha, "x-amz-date": amz_date})
    names = sorted(headers)
    canonical_query = "&".join(f"{quote(k, safe='-_.~')}={quote(str(v), safe='-_.~')}"
                               for k, v in sorted(query.items()))
    canonical = "\n".join([
        method, path, canonical_query,
        "".join(f"{n}:{headers[n]}\n" for n in names),
        ";".join(names), payload_sha,
    ])
    scope = f"{date}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    k = _hmac(("AWS4" + secret_key).encode(), date)
    for part in (region, "s3", "aws4_request"):
        k = _hmac(k, part)
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                                f"SignedHeaders={';'.join(names)}, Signature={sig}")
    del headers["host"]
    return headers


class S3Client:
    """put/get/head/list/delete on one bucket. Keys here are full object keys."""

    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str,
                 region: str = "auto", http: httpx.Client | None = None):
        self.endpoint = endpoint.rstrip("/")
        self.host = urlsplit(self.endpoint).netloc
        self.access_key, self.secret_key = access_key, secret_key
        self.bucket, self.region = bucket, region
        self.http = http or httpx.Client(timeout=30)

    def _request(self, method: str, key: str = "", query: dict | None = None,
                 body: bytes = b"", content_type: str | None = None,
                 conditions: dict | None = None) -> httpx.Response:
        path = "/" + quote(f"{self.bucket}/{key}" if key else self.bucket, safe="/-_.~")
        query = query or {}
        extra = {"content-type": content_type} if content_type else {}
        extra.update(conditions or {})
        headers = sign_headers(method, self.host, path, query, extra, hashlib.sha256(body).hexdigest(),
                               self.access_key, self.secret_key, self.region,
                               datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        # The query goes out exactly as it was signed (RFC 3986 escapes, space is %20, never '+').
        qs = "&".join(f"{quote(k, safe='-_.~')}={quote(str(v), safe='-_.~')}" for k, v in sorted(query.items()))
        return self.http.request(method, self.endpoint + path + ("?" + qs if qs else ""), content=body,
                                 headers=headers)

    @staticmethod
    def _check(r: httpx.Response, what: str, conditional: bool = False) -> None:
        if r.status_code in (409, 412) and what == "put" and conditional:   # only a refused condition is a fence
            raise PreconditionFailed(f"R2 {what} {r.status_code}: {r.text[:200]}")
        if r.status_code >= 300:
            raise RuntimeError(f"R2 {what} {r.status_code}: {r.text[:200]}")

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream",
            if_match: str | None = None, if_none_match: str | None = None) -> None:
        cond = {}
        if if_match:
            cond["If-Match"] = f'"{if_match}"'
        if if_none_match:
            cond["If-None-Match"] = if_none_match
        self._check(self._request("PUT", key, body=data, content_type=content_type, conditions=cond),
                    "put", conditional=bool(cond))

    def get(self, key: str) -> bytes | None:
        r = self._request("GET", key)
        if r.status_code == 404:
            return None
        self._check(r, "get")
        return r.content

    def head(self, key: str) -> dict | None:
        r = self._request("HEAD", key)
        if r.status_code == 404:
            return None
        self._check(r, "head")
        return {"size": int(r.headers.get("content-length", 0)), "etag": r.headers.get("etag", ""),
                "last_modified": r.headers.get("last-modified", "")}

    def list(self, prefix: str = "") -> list[dict]:
        out, token = [], None
        while True:
            q = {"list-type": "2", "prefix": prefix}
            if token:
                q["continuation-token"] = token
            r = self._request("GET", "", query=q)
            self._check(r, "list")
            root = ElementTree.fromstring(r.content)
            ns = {"s": root.tag.split("}")[0][1:]} if root.tag.startswith("{") else {}
            find = (lambda el, t: el.findtext(f"s:{t}", namespaces=ns)) if ns else (lambda el, t: el.findtext(t))
            for c in root.iter(f"{{{ns['s']}}}Contents" if ns else "Contents"):
                out.append({"key": find(c, "Key"), "size": int(find(c, "Size") or 0),
                            "last_modified": find(c, "LastModified")})
            if find(root, "IsTruncated") != "true":
                return out
            token = find(root, "NextContinuationToken")

    def delete(self, key: str) -> None:
        r = self._request("DELETE", key)
        if r.status_code != 404:
            self._check(r, "delete")


# ── Config ────────────────────────────────────────────────────────────────────

def _env() -> tuple[str, str, str, str]:
    return tuple(os.getenv(k, "").strip() for k in
                 ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"))


def set_client(client) -> None:
    """Tests: a fake with put/get/head/list/delete. None goes back to the env."""
    global _client, _built_from
    _client, _built_from = client, None


def _get_client():
    global _client, _built_from
    with _lock:
        cfg = _env()
        if _client is not None and _built_from is None:
            return _client                      # injected
        if not all(cfg):
            _client = _built_from = None
            return None
        if _built_from != cfg:
            _client, _built_from = S3Client(*cfg), cfg
        return _client


def configured() -> bool:
    return _get_client() is not None


def mode() -> str:
    return "R2" if configured() else "local-only"


def prefix() -> str:
    p = os.getenv("R2_PREFIX", "score/")
    return p if p == "" or p.endswith("/") else p + "/"


def budgets() -> dict:
    return {"class_a": int(os.getenv("R2_CLASS_A_BUDGET", "800000")),
            "class_b": int(os.getenv("R2_CLASS_B_BUDGET", "8000000"))}


# ── Accounting ────────────────────────────────────────────────────────────────

def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def usage(month: str | None = None) -> dict:
    row = db.query_one("SELECT class_a, class_b FROM r2_usage WHERE month = ?", (month or _month(),))
    return {"month": month or _month(), "class_a": row["class_a"] if row else 0,
            "class_b": row["class_b"] if row else 0}


def merge_usage(month: str, class_a: int, class_b: int) -> None:
    """Keep the larger counts: the db after a restore can lag the usage object in R2."""
    db.execute("INSERT INTO r2_usage (month, class_a, class_b) VALUES (?, ?, ?) ON CONFLICT(month) DO UPDATE "
               "SET class_a = MAX(class_a, excluded.class_a), class_b = MAX(class_b, excluded.class_b)",
               (month, class_a, class_b), track=False)


def exhausted(only: str | None = None) -> str | None:
    """"class_a"/"class_b" when this month's budget for it is spent (R2 mode only), else None.
    only checks that one class."""
    if not configured():
        return None
    u, b = usage(), budgets()
    for cls in ((only,) if only else ("class_a", "class_b")):
        if u[cls] >= b[cls]:
            key = (u["month"], cls)
            if key not in _warned:
                _warned.add(key)
                logger.warning("R2 %s budget spent (%d of %d) for %s: R2 I/O stopped, serving from "
                               "local disk until next month", cls, u[cls], b[cls], u["month"])
            return cls
    return None


def _count(cls: str) -> None:
    if exhausted(cls):
        raise Unavailable(f"R2 {cls} budget spent for {_month()}")
    record(cls)


def record(cls: str) -> None:
    """Count one request without checking the budget."""
    a, b = (1, 0) if cls == "class_a" else (0, 1)
    db.execute("INSERT INTO r2_usage (month, class_a, class_b) VALUES (?, ?, ?) ON CONFLICT(month) DO UPDATE "
               "SET class_a = class_a + excluded.class_a, class_b = class_b + excluded.class_b",
               (_month(), a, b), track=False)


def log_usage() -> None:
    u, b = usage(), budgets()
    logger.info("R2 usage %s: class A %d/%d, class B %d/%d", u["month"], u["class_a"], b["class_a"],
                u["class_b"], b["class_b"])


# ── Operations (keys are relative to R2_PREFIX; sync, call via to_thread) ─────

def _client_for(cls: str):
    client = _get_client()
    if client is None:
        raise Unavailable("R2 not configured")
    _count(cls)     # counted before the call: a failed request still costs one
    return client


def put(key: str, data: bytes, content_type: str = "application/octet-stream", *,
        if_match: str | None = None, if_none_match: str | None = None) -> None:
    """if_match = the md5 etag we last saw; if_none_match="*" = create only. A refusal raises
    PreconditionFailed (single-writer fencing for the snapshot pointer)."""
    cond = {}
    if if_match:
        cond["if_match"] = if_match
    if if_none_match:
        cond["if_none_match"] = if_none_match
    _client_for("class_a").put(prefix() + key, data, content_type, **cond)


def get(key: str) -> bytes | None:
    return _client_for("class_b").get(prefix() + key)


def head(key: str) -> dict | None:
    return _client_for("class_b").head(prefix() + key)


def list_keys(key_prefix: str = "") -> list[dict]:
    items = _client_for("class_a").list(prefix() + key_prefix)
    return [{**i, "key": i["key"][len(prefix()):]} for i in items]


def delete(key: str) -> None:
    client = _get_client()
    if client is None:
        raise Unavailable("R2 not configured")
    client.delete(prefix() + key)
