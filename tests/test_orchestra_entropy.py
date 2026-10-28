"""Session entropy (orchestra/entropy.py): baselines, events, bands, and trust earned from the score;
the backend's wiring; the entity's degradation (ui/face.degrade)."""

import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestra import entropy as en  # noqa: E402
from orchestra import events as ev  # noqa: E402
from orchestra import trust  # noqa: E402


def test_baselines_and_bands():
    from ui import face
    assert en.Entropy("claude-opus-5-5").band == "STABLE"
    assert en.Entropy("claude-sonnet-5-5").band == "STABLE"   # frontier tier: low entropy, trusted
    assert en.Entropy("claude-haiku-5-5").band == "ELEVATED"   # a weak model starts degraded
    for m in ("opus", "fable", "sonnet"):   # a /model switch costs a little, never trust or a visible tear
        e = en.Entropy(f"claude-{m}-5-5"); e.turns = 1; e.switch_model("claude-opus-5-5")
        assert e.band == "STABLE" and face.signal_level(e.score) < 0.12
    assert face.signal_level(en.Entropy("claude-opus-5-5").score) == 0
    e = en.Entropy("claude-opus-5-5"); e.turns = 1; e.switch_model("claude-sonnet-5-5")
    assert e.band == "STABLE"   # switching up to sonnet stays trusted
    sonnet = face.signal_level(en.Entropy("claude-sonnet-5-5").score)
    haiku = face.signal_level(en.Entropy("claude-haiku-5-5").score)
    assert 0.05 < sonnet < 0.2          # sonnet: a faint flicker, not clean
    assert 0.2 < haiku < 0.4            # haiku: clearly degraded but readable
    assert sonnet < haiku
    assert en.Entropy("mystery").score == en.UNKNOWN[0]
    assert [en.band_of(x) for x in (0, 0.2, 0.5, 0.7, 0.9, 1.0)] == \
        ["STABLE", "NOMINAL", "ELEVATED", "DEGRADED", "UNSTABLE", "UNSTABLE"]


def test_trust_is_earned_from_entropy():
    assert [en.trust_for_score(x) for x in (0.05, 0.25, 0.55, 0.95)] == ["trusted", "standard", "strict", "strict"]
    assert trust.profile_for("claude-haiku-5-5") == "strict"       # low-trust model = high entropy
    assert trust.profile_for("claude-opus-5-5") == "trusted"
    assert trust.profile_for("claude-opus-5-5", entropy=0.6) == "strict"   # a degraded session loses it
    assert trust.profile_for("claude-opus-5-5", "trusted", 0.95) == "trusted"  # explicit setting wins


def test_events_move_the_score():
    e = en.Entropy("claude-opus-5-5")
    e.turns = 1
    assert e.switch_model("claude-haiku-5-5")           # strong -> fast
    assert e.score == pytest.approx(0.55 + en.WEIGHTS["tier_degradation_medium_fast"])  # haiku baseline + the drop
    assert e.band == "DEGRADED"
    assert e.trust == "strict"


def test_tier_drop_weights():
    e = en.Entropy("claude-sonnet-5-5"); e.turns = 1
    e.switch_model("claude-haiku-5-5")
    assert e.events[-1][1] == en.WEIGHTS["tier_degradation_medium_fast"]
    e2 = en.Entropy("claude-haiku-5-5"); e2.turns = 1
    e2.switch_model("claude-opus-5-5")                   # upgrade is a plain switch
    assert e2.events[-1][1] == en.WEIGHTS["model_switch"]


def test_first_pick_is_free_and_events_dedupe():
    e = en.Entropy(None)
    assert e.switch_model("opus") is None and e.events == []
    e.turns = 1
    assert e.context(40) is None and e.context(20) is None          # small drop, below the compaction threshold
    assert e.context(80) and e.context(30) == "context compacted"
    assert e.rate_limited(True) and e.rate_limited(True) is None and e.rate_limited(False) is None
    for _ in range(25):
        e.turn_done()
    assert not any(k == "long_session_turn" for k, *_ in e.events)   # long sessions are pressure, not damage
    assert e.score <= 1.0


def test_pressure_rises_and_recovers_damage_does_not():
    e = en.Entropy("claude-opus-5-5"); e.turns = 1
    assert e.score == 0.05 and e.pressure == 0
    assert e.context(50) is None and e.pressure == 0                  # below the ramp: clean
    assert e.context(85) == "context 85% full" and e.band == "NOMINAL" and e.trust == "standard"
    e.context(95)
    assert e.pressure == pytest.approx(en.CONTEXT_MAX) and e.band == "NOMINAL"
    e._ctx = 20                                                         # window freed: pressure recovers
    assert e.pressure == 0 and e.band == "STABLE" and e.trust == "trusted"
    e.context(95); assert e.context(30) == "context compacted"        # compaction is damage, kept
    assert e.damage == en.WEIGHTS["context_truncation"] and e.score == pytest.approx(0.05 + 0.20 + 0.0)
    e2 = en.Entropy("claude-opus-5-5")
    e2.rate_limited("warning"); assert e2.pressure == 0.05
    e2.rate_limited("allowed"); assert e2.pressure == 0
    for _ in range(40):
        e2.turn_done()
    assert e2.pressure == en.LONG_MAX and e2.damage == 0


def test_backend_reports_and_trust_follows(monkeypatch):
    pytest.importorskip("claude_agent_sdk")
    from orchestra.backend import ClaudeCodeBackend
    b = ClaudeCodeBackend(".", model="opus")
    got = []
    b.emit = got.append
    b._observe(ev.SessionStarted("s", "claude-opus-5-5", "."))
    assert got[-1].band == "STABLE" and b.trust_profile() == "trusted"
    b._observe(ev.TurnDone("end_turn"))
    b.entropy.turns = 1
    b._report_entropy(b.entropy.switch_model("claude-haiku-5-5"))
    assert got[-1].trust == "strict" and "tier" in got[-1].reason
    assert b.trust_profile() == "strict"
    b.set_trust("trusted")
    assert b.trust_profile() == "trusted"


def test_degrade_clean_below_floor_and_worse_above():
    pytest.importorskip("textual")
    from ui import face
    pal = face.load_palette()
    cells = [[("█", pal["cyan"], pal["bg"])] * 24 for _ in range(6)]
    assert face.degrade(cells, 0.0, pal, random.Random(1), {}) == cells
    assert face.signal_level(0.05) == 0 and face.signal_level(1.0) == 1
    assert face.signal_level(0.3) < face.signal_level(0.6) < face.signal_level(0.9)   # monotonic

    def damage(level):
        st, rng, total = {}, random.Random(5), 0
        for _ in range(60):
            out = face.degrade(cells, level, pal, rng, st)
            total += sum(1 for row in out for cell in row if cell[0] != "█")
        return total
    assert damage(0.2) < damage(0.6) < damage(1.0)
    out = face.degrade(cells, 1.0, pal, random.Random(2), {})
    assert len(out) == 6 and all(len(r) == 24 for r in out)   # shape preserved


def test_fallback_and_fcc_switch():
    e = en.Entropy("claude-opus-5-5")
    assert e.model_used("claude-opus-5-5", None) == []                  # first sight
    reasons = e.model_used("claude-haiku-5-5", "claude-opus-5-5")        # nobody asked: fallback + tier drop
    assert len(reasons) == 2 and "fell back" in reasons[0]
    assert e.score == pytest.approx(0.55 + en.WEIGHTS["quota_forced_fallback"] + en.WEIGHTS["tier_degradation_medium_fast"])
    f = en.Entropy("deepseek-chat")
    assert "backend switched" in f.model_used("qwen3", "deepseek-chat")[0]
    assert [k for k, *_ in f.events] == ["fcc_backend_switch"]


def test_observer_switch_is_not_a_fallback():
    e = en.Entropy("claude-opus-5-5"); e.turns = 1
    e.switch_model("claude-sonnet-5-5")
    n = len(e.events)
    assert e.model_used("claude-sonnet-5-5", "claude-opus-5-5") == [] and len(e.events) == n
    e.turn_done()
    assert e.model_used("claude-haiku-5-5", "claude-sonnet-5-5")        # but the next unasked change counts


def test_errors_retry_storm_and_handoffs():
    e = en.Entropy("claude-opus-5-5")
    assert e.request_failed() and e.request_failed() is None and "retry storm" in e.request_failed()
    assert e.request_failed() is None
    e.turn_done()
    assert e.request_failed()                                           # new turn, counted again
    assert e.organ_brief("gobble", True) is None
    assert "without context" in e.organ_brief("gobble", False)
    assert e.events[-1][1] == en.WEIGHTS["implicit_handoff"]
    assert e.handoff() and e.events[-1][1] == en.WEIGHTS["model_switch"]


def test_thin_brief_detection():
    pytest.importorskip("claude_agent_sdk")
    from orchestra.sdk_map import _has_context
    assert not _has_context({"prompt": "map the beam reconnect logic"})
    assert _has_context({"prompt": "read RaBbLE-sCoRE/server/beam.py and map the reconnect logic"})
    assert _has_context({"prompt": "x" * 300})


def test_backend_wiring_and_carry():
    pytest.importorskip("claude_agent_sdk")
    from orchestra.backend import ClaudeCodeBackend
    b = ClaudeCodeBackend(".", model="opus")
    got = []
    b.emit = got.append
    b._observe(ev.ModelUsed("claude-opus-5-5"))
    b._observe(ev.ModelUsed("claude-haiku-5-5"))
    assert got[-1].trust == "strict" and "fell back" in got[-1].reason
    b._observe(ev.OrganStarted("gobble", "t1", None, "scout", False))
    assert "briefed without context" in got[-1].reason
    b._observe(ev.Error("rabble: rate_limit"))
    assert "request failed" in got[-1].reason
    seen = len(b.entropy.events)
    b2 = ClaudeCodeBackend(".", model="opus", entropy=b.entropy)      # engine restart on the same session
    got2 = []
    b2.emit = got2.append
    b2._observe(ev.SessionResumed("s"))
    assert len(b2.entropy.events) == seen + 1 and "context carried" in got2[-1].reason
