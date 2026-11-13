import json
import time
from datetime import datetime, timezone

from orchestra import usage


def write(path, five=7.0, week=13.0, age=60, resets_in=3600):
    ts = time.time() - age
    iso = datetime.fromtimestamp(time.time() + resets_in, timezone.utc).isoformat()
    path.write_text(json.dumps({"5h": {"ts": ts, "pct": five, "resets_at": iso},
                                "week": {"ts": ts, "pct": week, "resets_at": iso}}))
    return path


def test_reads_both_windows(tmp_path):
    u = usage.read(write(tmp_path / "u.json"))
    assert (u.five_hour.pct, u.week.pct) == (7.0, 13.0)
    assert not u.stale and 3500 < u.five_hour.resets_in() <= 3600
    assert u.pressure == 0.13


def test_stale_and_bands(tmp_path):
    u = usage.read(write(tmp_path / "u.json", five=95, age=5000))
    assert u.stale and u.five_hour.band == "high"
    assert usage.Window(75, None).band == "warn" and usage.Window(10, None).band == "ok"


def test_missing_or_garbage_is_empty(tmp_path):
    for body in (None, "not json", "[]", '{"5h": {"pct": "x"}}'):
        p = tmp_path / "u.json"
        p.unlink(missing_ok=True)
        if body:
            p.write_text(body)
        u = usage.read(p)
        assert u.five_hour is None and u.stale and u.pressure == 0.0


def test_fmt_remaining():
    assert [usage.fmt_remaining(x) for x in (None, 120, 4 * 3600 + 21 * 60, 2 * 86400 + 3 * 3600)] == ["?", "2m", "4h21m", "2d3h"]
