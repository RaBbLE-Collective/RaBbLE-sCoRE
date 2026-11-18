"""
Outgoing mail for account verification and password reset.

Mailer.send(to, subject, text) is the whole interface. With no MAIL_PROVIDER the
mailer is NullMailer: nothing is sent and nothing sensitive is logged (a public deploy without a
real sender must not leak single-use links into its logs). ConsoleMailer prints the message, link
and token included, and is used only when asked for explicitly with MAIL_PROVIDER=console (laptop
or test run, never a public deploy).

Seam for a real sender: subclass Mailer, then register it in _SENDERS under a MAIL_PROVIDER name
(an SMTP or HTTPS-API sender goes here; its credentials come from MAIL_* env). get_mailer() picks it.
An unknown MAIL_PROVIDER, or MAIL_* set with no provider, raises: a misconfigured prod must not
quietly fall back.

ResendMailer (MAIL_PROVIDER=resend) posts to Resend's HTTPS API. It needs RESEND_API_KEY and MAIL_FROM,
and get_mailer() raises RuntimeError naming whichever is missing. It retries once on a network error or
5xx, never on 4xx, and raises on failure. It logs only the status, Resend's error name and the subject:
never the key, the message, the link or the recipient.

Env: MAIL_PROVIDER (name in _SENDERS, or "console"; unset = nothing is sent), MAIL_FROM (sender
address, for real senders), RESEND_API_KEY (secret, for "resend").
"""

import logging
import os
import time

import httpx

logger = logging.getLogger("rabble.mailer")


class Mailer:
    def send(self, to: str, subject: str, text: str) -> None:
        raise NotImplementedError


class NullMailer(Mailer):
    """The default: drops the message. Logs that mail is off, never the message or its link."""

    def send(self, to: str, subject: str, text: str) -> None:
        logger.warning("mail not sent (no MAIL_PROVIDER set): subject=%s", subject)


class ConsoleMailer(Mailer):
    """Explicit opt-in only (MAIL_PROVIDER=console). Prints the whole message, token link included."""

    def __init__(self):
        self.outbox: list[dict] = []   # tests read the last message here

    def send(self, to: str, subject: str, text: str) -> None:
        self.outbox.append({"to": to, "subject": subject, "text": text})
        del self.outbox[:-200]
        print(f"[mail:console] to={to} subject={subject}\n{text}", flush=True)


_RESEND_URL = "https://api.resend.com/emails"


def _post(url: str, headers: dict, body: dict, timeout: float) -> tuple[int, dict]:
    """The only network call of ResendMailer (tests replace it). Returns (status, json or {})."""
    r = httpx.post(url, headers=headers, json=body, timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        data = {}
    return r.status_code, data if isinstance(data, dict) else {}


class ResendMailer(Mailer):
    """MAIL_PROVIDER=resend. Needs RESEND_API_KEY and MAIL_FROM; a missing one raises at construction."""

    def __init__(self):
        for name in ("RESEND_API_KEY", "MAIL_FROM"):
            if not os.getenv(name, "").strip():
                raise RuntimeError(f"MAIL_PROVIDER=resend needs {name}")
        self._key = os.environ["RESEND_API_KEY"].strip()
        self._from = os.environ["MAIL_FROM"].strip()

    def send(self, to: str, subject: str, text: str) -> None:
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        body = {"from": self._from, "to": [to], "subject": subject, "text": text}
        for attempt in (1, 2):
            try:
                status, data = _post(_RESEND_URL, headers, body, 10)
            except Exception as e:   # network error: log the type only, never the message
                status, data = 0, {}
                logger.warning("resend network error (%s): subject=%s", type(e).__name__, subject)
            else:
                if 200 <= status < 300:
                    return
                detail = str(data.get("name") or data.get("message") or "")[:100]
                logger.warning("resend failed: status=%s error=%s subject=%s", status, detail, subject)
            if attempt == 1 and (status == 0 or status >= 500):
                time.sleep(1)
                continue
            break
        raise RuntimeError(f"resend send failed (status {status})")


_SENDERS: dict[str, type[Mailer]] = {"resend": ResendMailer}
_console = ConsoleMailer()
_null = NullMailer()
_override: Mailer | None = None          # tests


def set_mailer(m: Mailer | None) -> None:
    global _override
    _override = m


def get_mailer() -> Mailer:
    if _override is not None:
        return _override
    provider = os.getenv("MAIL_PROVIDER", "").strip().lower()
    if not provider:
        if any(k.startswith("MAIL_") and k != "MAIL_PROVIDER" and os.getenv(k) for k in os.environ):
            raise RuntimeError("MAIL_* is set but MAIL_PROVIDER is not; refusing to fall back to the console mailer")
        return _null
    if provider == "console":
        return _console
    cls = _SENDERS.get(provider)
    if cls is None:
        raise RuntimeError(f"unknown MAIL_PROVIDER {provider!r}")
    return cls()
