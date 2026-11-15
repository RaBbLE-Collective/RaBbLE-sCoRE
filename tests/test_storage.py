"""R2 storage: SigV4, request accounting, budgets, local-only mode. No network."""

import pytest

import db
import storage


def test_sigv4_matches_aws_documented_example():
    # "GET Object" example from the AWS SigV4 docs (header-based auth, Range header).
    h = storage.sign_headers(
        "GET", "examplebucket.s3.amazonaws.com", "/test.txt", {}, {"Range": "bytes=0-9"},
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1", "20130524T000000Z")
    assert h["authorization"].endswith(
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in h["authorization"]


def test_local_only_when_unset(data_dir):
    assert storage.mode() == "local-only"
    assert not storage.configured()
    with pytest.raises(storage.Unavailable):
        storage.put("a", b"x")
    assert storage.exhausted() is None
    assert storage.usage()["class_a"] == 0   # nothing counted


def test_env_builds_a_client(data_dir, monkeypatch):
    for k, v in {"R2_ENDPOINT": "https://x.r2.cloudflarestorage.com", "R2_ACCESS_KEY_ID": "k",
                 "R2_SECRET_ACCESS_KEY": "s", "R2_BUCKET": "b"}.items():
        monkeypatch.setenv(k, v)
    assert storage.mode() == "R2"
    assert isinstance(storage._get_client(), storage.S3Client)


def test_prefix_and_counting(r2, monkeypatch):
    storage.put("snapshots/score.db", b"abc")
    assert r2.objects == {"score/snapshots/score.db": b"abc"}
    assert storage.get("snapshots/score.db") == b"abc"
    assert storage.get("missing") is None
    assert storage.head("snapshots/score.db")["size"] == 3
    assert [i["key"] for i in storage.list_keys("snap")] == ["snapshots/score.db"]
    storage.delete("snapshots/score.db")
    u = storage.usage()
    assert (u["class_a"], u["class_b"]) == (2, 3)    # put + list; get, get, head. delete is free
    monkeypatch.setenv("R2_PREFIX", "other")
    storage.put("k", b"1")
    assert "other/k" in r2.objects


def test_failed_requests_still_count(r2):
    r2.fail = True
    with pytest.raises(RuntimeError):
        storage.put("k", b"x")
    assert storage.usage()["class_a"] == 1


def test_budget_stops_io_warns_once_and_resumes_next_month(r2, monkeypatch, caplog):
    monkeypatch.setenv("R2_CLASS_A_BUDGET", "2")
    monkeypatch.setenv("R2_CLASS_B_BUDGET", "1")
    storage.put("a", b"1")
    storage.put("b", b"2")
    assert storage.exhausted() == "class_a"
    caplog.clear()
    with caplog.at_level("WARNING", logger="rabble.storage"):
        for _ in range(3):
            with pytest.raises(storage.Unavailable):
                storage.put("c", b"3")
    assert len(r2.calls) == 2                               # the blocked puts never reached R2
    storage.get("a")
    with pytest.raises(storage.Unavailable):
        storage.get("a")
    # a new month starts from zero
    monkeypatch.setattr(storage, "_month", lambda: "2099-01")
    storage.put("d", b"4")
    assert storage.exhausted() is None
    assert storage.usage()["class_a"] == 1


def test_warning_logged_once_per_month(r2, monkeypatch, caplog):
    monkeypatch.setenv("R2_CLASS_A_BUDGET", "1")
    storage.put("a", b"1")
    with caplog.at_level("WARNING", logger="rabble.storage"):
        for _ in range(4):
            storage.exhausted()
    assert len([r for r in caplog.records if "budget spent" in r.message]) == 1


def test_merge_usage_keeps_the_larger_count(r2):
    storage.put("a", b"1")
    storage.merge_usage(storage._month(), 10, 0)
    storage.merge_usage(storage._month(), 3, 7)
    u = storage.usage()
    assert (u["class_a"], u["class_b"]) == (10, 7)


def test_signed_query_is_sent_as_signed_with_spaces_as_percent_20():
    import httpx
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, content=b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                                           b"<IsTruncated>false</IsTruncated></ListBucketResult>")
    c = storage.S3Client("https://a.r2.cloudflarestorage.com", "k", "s", "bkt",
                         http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.list("score/a b*+") == []
    assert seen["url"].endswith("/bkt?list-type=2&prefix=score%2Fa%20b%2A%2B")
    assert "+" not in seen["url"].split("?")[1].replace("%2B", "")
    assert "Signature=" in seen["auth"]
