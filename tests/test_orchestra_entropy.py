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


def test_capabilities_and_tiers():
    from ui import face
    got = {m: en.Entropy(m) for m in ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5",
                                      "claude-haiku-5-5", "qwen3-coder:30b", "mystery")}
    assert [e.capability() for e in got.values()] == [95, 85, 70, 35, 25, 25]
    assert [e.trust for e in got.values()] == ["trusted", "trusted", "trusted", "strict", "strict", "strict"]
    assert got["qwen3-coder:30b"].local and not got["mystery"].local and not got["claude-opus-5-5"].local
    assert all(e.band == "STABLE" for e in got.values())       # nothing lost yet: strength is not damage
    assert got["mystery"].score == 0.75
    for m in ("opus", "fable", "sonnet"):   # a /model switch costs a little, never trust or a visible tear
        e = en.Entropy(f"claude-{m}-5-5"); e.turns = 1; e.switch_model("claude-opus-5-5")
        assert e.trust == "trusted" and face.signal_level(e.score) < 0.12
    assert face.signal_level(got["claude-opus-5-5"].score) == 0
    sonnet = face.signal_level(got["claude-sonnet-5-5"].score)
    haiku = face.signal_level(got["claude-haiku-5-5"].score)
    local = face.signal_level(got["qwen3-coder:30b"].score)
    assert 0.05 < sonnet < 0.2 and 0.2 < haiku < 0.4 and haiku < local   # faint, degraded, worse
    assert [en.band_of(x) for x in (0, 0.10, 0.25, 0.40, 0.60, 1.0)] == \
        ["STABLE", "NOMINAL", "ELEVATED", "DEGRADED", "UNSTABLE", "UNSTABLE"]


def test_categories_differ_by_model_and_by_damage():
    opus, sonnet = en.Entropy("claude-opus-5-5"), en.Entropy("claude-sonnet-5-5")
    assert opus.capabilities() == {"general": 85, "coding": 88, "tools": 86, "recall": 84}
    assert sonnet.capability("coding") > sonnet.capability("general")        # a coder first
    opus.turns = 1
    opus.context(85)                                                          # bloat: recall and coding pay more
    c = opus.capabilities()
    assert 85 - c["general"] < 88 - c["coding"] < 84 - c["recall"]
    assert c["tools"] > c["recall"] and c["general"] < c["tools"]       # tools are not hurt by bloat the way recall is
    f = en.Entropy("claude-opus-5-5"); f.rate_limited("warning")
    assert 86 - f.capability("tools") > 85 - f.capability("general")          # faults hit tools harder


def test_registry_can_be_overridden_per_user(tmp_path, monkeypatch):
    from orchestra import capabilities as caps
    mine = tmp_path / "caps.json"
    mine.write_text('{"models": [{"key": "opus", "coding": 60, "source": "measured"},'
                    ' {"key": "mylocal", "match": ["mylocal"], "general": 55, "coding": 80, "tools": 50,'
                    ' "recall": 55, "local": true}]}')
    monkeypatch.setenv("RABBLE_CAPABILITIES", str(mine))
    caps.reload()
    try:
        opus = en.Entropy("claude-opus-5-5")
        assert opus.capability("coding") == 60 and opus.capability("general") == 85 and opus.cap.source == "measured"
        mine_ = en.Entropy("mylocal-7b")
        assert mine_.local and mine_.capability("general") == 55 and mine_.trust == "trusted"   # weak at chat, good at code
    finally:
        monkeypatch.delenv("RABBLE_CAPABILITIES")
        caps.reload()


def test_trust_is_earned_from_capability():
    assert [en.trust_for_score(x) for x in (0.15, 0.35, 0.55, 0.75)] == ["trusted", "trusted", "standard", "strict"]
    assert trust.profile_for("claude-haiku-5-5") == "strict"       # low-capability model
    assert trust.profile_for("claude-opus-5-5") == "trusted"
    assert trust.profile_for("claude-opus-5-5", entropy=0.7) == "strict"   # a degraded session loses it
    assert trust.profile_for("claude-opus-5-5", "trusted", 0.95) == "trusted"  # explicit setting wins


def test_events_move_the_score():
    e = en.Entropy("claude-opus-5-5")
    e.turns = 1
    assert e.switch_model("claude-haiku-5-5")           # strong -> fast
    assert e.score == pytest.approx(1 - (0.35 - en.WEIGHTS["tier_degradation_medium_fast"]))   # haiku's 35 minus the drop
    assert e.band == "ELEVATED" and e.lost == pytest.approx(en.WEIGHTS["tier_degradation_medium_fast"])
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
    assert e.score == 0.15 and e.pressure == 0
    assert e.context(50) is None and e.pressure == 0                  # below the ramp: clean
    assert e.context(85) == "context 85% full" and e.band == "NOMINAL" and e.trust == "trusted"
    e.context(95)
    assert e.pressure == pytest.approx(en.CONTEXT_MAX) and e.band == "ELEVATED" and e.trust == "standard"
    e._ctx = 20                                                         # window freed: pressure recovers
    assert e.pressure == 0 and e.band == "STABLE" and e.trust == "trusted"
    e.context(95); assert e.context(30) == "context compacted"        # compaction is damage, kept
    assert e.damage == en.WEIGHTS["context_truncation"] and e.score == pytest.approx(0.15 + 0.20 + 0.0)
    e2 = en.Entropy("claude-opus-5-5")
    e2.rate_limited("warning"); assert e2.pressure == 0.05
    e2.rate_limited("allowed"); assert e2.pressure == 0
    for _ in range(40):
        e2.turn_done()
    assert e2.pressure == en.LONG_MAX and e2.damage == 0


def test_transient_damage_decays_with_clean_turns_structural_does_not():
    e = en.Entropy("claude-opus-5-5"); e.turns = 1
    e.request_failed()                                  # transient
    first = e.damage
    assert first == pytest.approx(en.WEIGHTS["provider_instability"])
    e.request_failed()
    e.turn_done()                                       # a turn WITH a failure: no decay yet
    assert e.damage == pytest.approx(first)
    for _ in range(3):
        e.turn_done()                                   # clean turns
    assert e.damage == pytest.approx(first * en.DECAY ** 3)
    for _ in range(30):
        e.turn_done()
    assert e.damage < 0.001                             # the fault has passed
    s = en.Entropy("claude-opus-5-5"); s.turns = 1
    s.context(90); s.context(30)                        # compaction: structural
    before = s.damage
    for _ in range(30):
        s.turn_done()
    assert s.damage == pytest.approx(before)


def test_regrounding_earns_back_half_the_structural_damage_at_most():
    e = en.Entropy("claude-opus-5-5"); e.turns = 1
    assert e.reground("read", "a.py") is False          # nothing lost: nothing to earn
    e.context(90); e.context(30)                        # compaction: 0.20 structural
    base = e.damage
    assert base == pytest.approx(0.20)
    assert e.reground("read", "a.py") and e.damage == pytest.approx(0.20 * (1 - 0.02 / 0.20))
    assert e.reground("read", "a.py") is False          # the same file earns once
    assert e.reground("verify") and e.reground("verify")
    for i in range(30):
        e.reground("read", f"f{i}.py")
    assert e.damage == pytest.approx(0.20 * 0.5)        # capped: parking is still the only full recovery
    e.context(90); e.context(30)                        # more loss re-opens files for credit
    assert e.damage > 0.20 * 0.5 and e.reground("read", "a.py")


def test_park_restores_everything():
    old = en.Entropy("claude-opus-5-5"); old.turns = 1
    old.context(90); old.context(30)
    fresh = en.Entropy("claude-opus-5-5")               # what a parked session starts from
    fresh.handoff()
    assert fresh.capability() > old.capability()
    assert fresh.damage == pytest.approx(en.WEIGHTS["model_switch"])


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
    assert e.score == 1.0 and e.capability() == 0     # haiku's 35 minus both penalties bottoms out
    assert e.lost == pytest.approx(en.WEIGHTS["quota_forced_fallback"] + en.WEIGHTS["tier_degradation_medium_fast"])
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


def test_backend_regrounds_on_reads_and_passing_tests():
    pytest.importorskip("claude_agent_sdk")
    from orchestra.backend import ClaudeCodeBackend
    b = ClaudeCodeBackend(".", model="opus")
    b.emit = lambda e: None
    b.entropy.turns = 1
    b.entropy.context(90); b.entropy.context(30)                      # compaction
    d0 = b.entropy.damage
    b._observe(ev.ToolCall("rabble", "t1", "Read", {"file_path": "/x/a.py"}))
    d1 = b.entropy.damage
    assert d1 < d0                                                       # a re-read earns credit
    b._observe(ev.ToolCall("rabble", "t1b", "Read", {"file_path": "/x/a.py"}))
    assert b.entropy.damage == d1                                        # the same file only once
    b._observe(ev.ToolCall("rabble", "t2", "Bash", {"command": "pytest -q tests"}))
    b._observe(ev.ToolResult("rabble", "t2", False, "1 failed"))
    assert b.entropy.damage == d1                                        # a failing run earns nothing
    b._observe(ev.ToolCall("rabble", "t3", "Bash", {"command": "cd x && pytest -q"}))
    b._observe(ev.ToolResult("rabble", "t3", True, "passed"))
    assert b.entropy.damage < d1                                         # a passing run does
    b._observe(ev.ToolCall("rabble", "t4", "Bash", {"command": "ls"}))
    b._observe(ev.ToolResult("rabble", "t4", True, "ok"))
    assert b._calls == {}                                                # nothing is left tracked


def test_entropy_event_carries_capability():
    pytest.importorskip("claude_agent_sdk")
    from orchestra.backend import ClaudeCodeBackend
    b = ClaudeCodeBackend(".", model="opus")
    got = []
    b.emit = got.append
    b._observe(ev.SessionStarted("s", "claude-opus-5-5", "."))
    e = got[-1]
    assert e.capability == 85 and e.categories["coding"] == 88 and e.local is False
    assert ev.from_json(e.to_json()) == e                                # round-trips
