"""Snapshot persistence: timestamped snapshots, pointer, verification, shrink gate, prune, and the
rule that nothing ever silently runs empty. Fake R2."""

import asyncio
import json

import pytest

import db
import snapshot
import storage
import users
from users import UserProfile

P = "score/"
POINTER = P + "snapshots/latest.json"


def _user(i="u1"):
    return UserProfile(id=i, handle="h" + i, display_name="N", join_date="2026-01-01", intention="",
                       api_key="k" + i)


def _snap_keys(r2):
    return sorted(k for k in r2.objects if k.startswith(P + "snapshots/score-"))


def _puts(r2):
    return [k for op, k in r2.calls if op == "put"]


@pytest.fixture()
def fresh(r2, monkeypatch):
    """An empty bucket and the explicit first-boot flag."""
    monkeypatch.setenv("R2_ALLOW_FRESH_START", "1")
    return r2


def _seed(r2, n=1):
    """n snapshots in the bucket from an earlier life, then the disk is gone."""
    for i in range(n):
        users.create_user(_user(f"s{i}"))
        assert snapshot.flush()
    db.close()
    db.db_path().unlink()
    db.clear_dirty()
    snapshot.state, snapshot.hold, snapshot._known, snapshot._marker_done = "disabled", None, None, False
    snapshot.baseline, snapshot.latest = {"users": 0, "invites": 0}, {}


def test_local_only_does_nothing(data_dir):
    users.create_user(_user())
    assert snapshot.flush() is False
    assert snapshot.try_restore() == "disabled"


def test_each_upload_is_a_new_key_with_pointer_marker_and_usage(fresh):
    assert snapshot.try_restore() == "fresh"
    users.create_user(_user("a"))
    assert snapshot.flush() is True
    users.create_user(_user("b"))
    assert snapshot.flush() is True
    keys = _snap_keys(fresh)
    assert len(keys) == 2 and keys[0] != keys[1]                    # nothing overwritten
    assert all(k.endswith("Z.db") and "score-2" in k for k in keys)
    ptr = json.loads(fresh.objects[POINTER])
    assert P + ptr["key"] == keys[-1] and ptr["users"] == 2 and len(ptr["sha256"]) == 64
    assert P + "snapshots/INITIALIZED" in fresh.objects
    assert P + "accounting/usage.json" in fresh.objects
    assert db.dirty_since is None and snapshot.flush() is False     # unchanged: no upload


def test_restore_reads_the_pointer(fresh):
    snapshot.try_restore()
    _seed(fresh, 2)
    assert snapshot.try_restore() == "restored"
    assert users.get_user("s0") and users.get_user("s1")
    assert snapshot.baseline["users"] == 2


def test_corrupt_latest_object_falls_back_to_an_older_snapshot(fresh):
    snapshot.try_restore()
    _seed(fresh, 3)
    newest = _snap_keys(fresh)[-1]
    fresh.objects[newest] = b"this is not a sqlite database"
    assert snapshot.try_restore() == "restored"
    assert users.get_user("s1") is not None and users.get_user("s2") is None   # the second newest was used


def test_sha_mismatch_and_missing_object_fall_back(fresh):
    snapshot.try_restore()
    _seed(fresh, 3)
    keys = _snap_keys(fresh)
    good = fresh.objects[keys[0]]
    fresh.objects[keys[-1]] = good                                  # valid sqlite, but not what the pointer hashed
    del fresh.objects[keys[1]]                                      # and one missing
    assert snapshot.try_restore() == "restored"
    assert users.get_user("s0") is not None and users.get_user("s1") is None


def test_corrupt_pointer_uses_the_newest_snapshot(fresh):
    snapshot.try_restore()
    _seed(fresh, 2)
    fresh.objects[POINTER] = b"{ nope"
    assert snapshot.try_restore() == "restored"
    assert users.get_user("s1") is not None


def test_all_snapshots_corrupt_is_a_failure_never_fresh(fresh):
    snapshot.try_restore()
    _seed(fresh, 2)
    for k in _snap_keys(fresh):
        fresh.objects[k] = b"garbage"
    assert snapshot.try_restore() == "failed"
    assert not snapshot.uploads_enabled()


def test_integrity_check_gate_on_restore_and_upload():
    assert snapshot.inspect(b"junk")[0] is False


def test_integrity_failure_blocks_the_upload(fresh, monkeypatch):
    snapshot.try_restore()
    users.create_user(_user())
    monkeypatch.setattr(snapshot, "inspect", lambda data: (False, 0, 0))
    assert snapshot.flush() is False
    assert "integrity" in snapshot.hold and _snap_keys(fresh) == []


def test_shrink_is_refused_then_allowed_by_the_admin_flag(fresh, monkeypatch, caplog):
    snapshot.try_restore()
    for i in range(12):
        users.create_user(_user(f"u{i}"))
    assert snapshot.flush() is True
    before = _snap_keys(fresh)
    db.execute("DELETE FROM users WHERE id IN ('u0', 'u1', 'u2', 'u3', 'u4', 'u5')")   # a wipe
    with caplog.at_level("ERROR", logger="rabble.snapshot"):
        assert snapshot.flush() is False
    assert "UPLOAD REFUSED" in caplog.text and "6 users" in snapshot.hold
    assert not snapshot.uploads_enabled() and _snap_keys(fresh) == before
    assert db.dirty_since is not None                                # still pending, on hold
    db.execute("DELETE FROM users WHERE id = 'u6'")
    monkeypatch.setenv("SNAPSHOT_ALLOW_SHRINK", "1")
    assert snapshot.flush() is True
    assert snapshot.hold is None and len(_snap_keys(fresh)) == 2


def test_a_small_shrink_inside_ten_percent_is_fine(fresh):
    snapshot.try_restore()
    for i in range(20):
        users.create_user(_user(f"u{i}"))
    snapshot.flush()
    db.execute("DELETE FROM users WHERE id = 'u0'")
    assert snapshot.flush() is True


def test_prune_keeps_n_and_never_the_newest_or_the_pointer(fresh, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_KEEP", "3")
    snapshot.try_restore()
    for i in range(6):
        users.create_user(_user(f"u{i}"))
        assert snapshot.flush()
    keys = _snap_keys(fresh)
    assert len(keys) == 3
    assert P + json.loads(fresh.objects[POINTER])["key"] == keys[-1]
    # the pointer names an OLD object (say a later pointer write failed): it survives a prune
    old = snapshot.latest["key"] = keys[0][len(P):]
    snapshot._known = [k[len(P):] for k in keys]
    snapshot._prune(keys[-1][len(P):])
    assert keys[0] in fresh.objects
    # nothing is deleted when there is nothing beyond N, and prune with keep=1 still keeps the newest
    monkeypatch.setenv("SNAPSHOT_KEEP", "1")
    snapshot.latest["key"] = keys[-1][len(P):]
    snapshot._prune(keys[-1][len(P):])
    assert _snap_keys(fresh) == [keys[-1]]


def test_failed_upload_prunes_nothing(fresh, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_KEEP", "1")
    snapshot.try_restore()
    for i in range(2):
        users.create_user(_user(f"u{i}"))
        snapshot.flush()
    kept = _snap_keys(fresh)
    users.create_user(_user("x"))
    fresh.fail = True
    assert snapshot.flush() is False
    fresh.fail = False
    assert _snap_keys(fresh) == kept and "x" not in str(fresh.calls[-3:])


def test_extra_requests_are_counted(fresh):
    snapshot.try_restore()
    base = storage.usage()
    users.create_user(_user())
    snapshot.flush()
    u = storage.usage()
    # snapshot + pointer + marker + usage object = 4 puts, plus the prune LIST
    assert u["class_a"] - base["class_a"] == 5
    assert base["class_a"] >= 2 and base["class_b"] >= 1            # the boot checks were counted too


def test_existing_local_db_is_not_overwritten(fresh):
    snapshot.try_restore()
    users.create_user(_user())
    snapshot.flush()
    users.create_user(_user("u2"))
    db.close()
    assert snapshot.try_restore() == "present"
    assert users.get_user("u2") is not None


def test_budget_spent_stops_uploads_and_stays_dirty(fresh, monkeypatch):
    snapshot.try_restore()
    monkeypatch.setenv("R2_CLASS_A_BUDGET", str(storage.usage()["class_a"] + 1))
    users.create_user(_user())
    assert snapshot.flush() is False
    assert db.dirty_since is not None


def test_failed_upload_retries_later(fresh):
    snapshot.try_restore()
    users.create_user(_user())
    fresh.fail = True
    assert snapshot.flush() is False and db.dirty_since is not None
    fresh.fail = False
    assert snapshot.flush() is True


def test_stop_flushes_once_on_shutdown(fresh):
    async def go():
        await snapshot.start()
        users.create_user(_user())
        await snapshot.stop()
    asyncio.run(go())
    assert len(_snap_keys(fresh)) == 1


# ── nothing ever silently runs empty ──────────────────────────────────────────

def test_404_without_the_flag_is_a_failure_and_uploads_stay_off(r2, caplog):
    with caplog.at_level("ERROR", logger="rabble.snapshot"):
        assert snapshot.try_restore() == "failed"
    assert "R2_ALLOW_FRESH_START" in caplog.text
    users.create_user(_user())
    assert snapshot.flush() is False and _puts(r2) == []
    # the process restarts on the same disk: the db now exists, but the failure is remembered
    snapshot.state = "disabled"
    assert snapshot.try_restore() == "failed"
    assert snapshot.flush() is False and _puts(r2) == []


def test_flag_with_an_existing_snapshot_elsewhere_is_refused(r2, monkeypatch):
    monkeypatch.setenv("R2_ALLOW_FRESH_START", "1")
    r2.objects["other-prefix/snapshots/score-20260101T000000Z.db"] = b"x"
    assert snapshot.try_restore() == "failed"
    assert "snapshot-looking" in db.db_path().with_name(".restore-pending").read_text()
    assert snapshot.flush() is False


def test_flag_with_a_lone_pointer_elsewhere_is_refused(r2, monkeypatch):
    monkeypatch.setenv("R2_ALLOW_FRESH_START", "1")
    r2.objects["wrong/snapshots/latest.json"] = b"{}"
    assert snapshot.try_restore() == "failed"


def test_marker_present_means_never_fresh_even_with_the_flag(r2, monkeypatch):
    monkeypatch.setenv("R2_ALLOW_FRESH_START", "1")
    r2.objects[P + "snapshots/INITIALIZED"] = b"2026"
    assert snapshot.try_restore() == "failed"
    assert "INITIALIZED" in db.db_path().with_name(".restore-pending").read_text()


def test_clean_empty_bucket_with_the_flag_starts_fresh(fresh):
    r2_calls_before = len(fresh.calls)
    assert snapshot.try_restore() == "fresh"
    assert snapshot.uploads_enabled()
    assert not db.db_path().with_name(".restore-pending").exists()
    assert ("list", "") in fresh.calls[r2_calls_before:]            # the bucket root was checked
    assert ("list", P + "snapshots/score-") in fresh.calls
    users.create_user(_user())
    assert snapshot.flush() is True and P + "snapshots/INITIALIZED" in fresh.objects


def test_after_the_first_upload_a_missing_pointer_is_never_fresh(fresh, monkeypatch):
    snapshot.try_restore()
    users.create_user(_user())
    snapshot.flush()
    db.close()
    db.db_path().unlink()
    for k in list(fresh.objects):
        if "snapshots/score-" in k or k.endswith("latest.json"):
            del fresh.objects[k]                                    # someone deleted the snapshots
    snapshot.state = "disabled"
    assert snapshot.try_restore() == "failed"                       # flag still set, marker still there


# ── a failed restore must never become an upload ──────────────────────────────

class _FlakyGet:
    """Wraps FakeR2: gets fail on demand and note whether the db already existed."""

    def __init__(self, fake):
        self.fake, self.fail_get, self.db_existed_at_get = fake, True, []

    def get(self, key):
        self.db_existed_at_get.append(db.db_path().exists())
        if self.fail_get:
            raise RuntimeError("503 from R2")
        return self.fake.get(key)

    def __getattr__(self, name):
        return getattr(self.fake, name)


def test_failed_restore_never_uploads_an_empty_db_then_retry_recovers(fresh, caplog):
    snapshot.try_restore()
    _seed(fresh)
    good = dict((k, v) for k, v in fresh.objects.items() if "snapshots/" in k)
    flaky = _FlakyGet(fresh)
    storage.set_client(flaky)
    fresh.calls.clear()
    with caplog.at_level("ERROR", logger="rabble.snapshot"):
        assert snapshot.try_restore() == "failed"
    assert "RESTORE FAILED" in caplog.text
    assert flaky.db_existed_at_get[0] is False                      # the GET ran before the db existed
    users.create_user(_user("new"))
    assert snapshot.flush() is False and _puts(fresh) == []
    assert all(fresh.objects[k] == v for k, v in good.items())     # the R2 copies are untouched
    flaky.fail_get = False
    assert snapshot.try_restore() == "restored"
    assert users.get_user("s0") is not None and users.get_user("new") is not None
    assert snapshot.flush() is True


def test_budget_spent_at_retry_keeps_uploads_off(fresh, monkeypatch):
    snapshot.try_restore()
    _seed(fresh)
    flaky = _FlakyGet(fresh)
    storage.set_client(flaky)
    assert snapshot.try_restore() == "failed"
    flaky.fail_get = False
    monkeypatch.setenv("R2_CLASS_B_BUDGET", str(storage.usage()["class_b"]))
    assert snapshot.try_restore() == "failed"
    assert snapshot.uploads_enabled() is False


def test_start_retries_the_restore_in_the_background(fresh, monkeypatch):
    snapshot.try_restore()
    _seed(fresh)
    flaky = _FlakyGet(fresh)
    storage.set_client(flaky)
    monkeypatch.setattr(snapshot, "RETRY_S", 0)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(snapshot.asyncio, "sleep", lambda _: real_sleep(0.01))

    async def go():
        await snapshot.start()
        assert snapshot.state == "failed"
        flaky.fail_get = False
        for _ in range(100):
            await real_sleep(0.01)
            if snapshot.state != "failed":
                break
        await snapshot.stop()
    asyncio.run(go())
    assert snapshot.state == "restored" and users.get_user("s0") is not None
