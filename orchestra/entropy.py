"""Session entropy: one 0.0-1.0 score for how far to trust what this session produces.
Implements the score and bands of RaBbLE-Grimoire/RaBbLE-sCoRE/RaBbLE-sCoRE-Entropy-Tracker.md
for the UI engine (the doc's server/entropy.py does not exist yet).

score = the model's baseline + the deltas of session events (tier drops, context
compaction, rate-limit trouble, long sessions). A low-trust model *is* a high-entropy
one, so the baseline is where model trust lives: the trust profile (orchestra/trust.py)
and the entity's glitch (ui/face.py) both read this one number.

Pure data: no SDK, no UI, deterministic.
"""

from dataclasses import dataclass, field

# (band, upper bound exclusive): the doc's table
BANDS = (("STABLE", 0.2), ("NOMINAL", 0.5), ("ELEVATED", 0.7), ("DEGRADED", 0.9), ("UNSTABLE", 1.01))
# model-name fragment -> (baseline entropy, capability tier). First match wins. "gpt" is for the
# future GPT backend (plan U5). Unknown models get the middle of the road.
FAMILIES = (("fable", 0.05, "strong"), ("opus", 0.05, "strong"), ("gpt", 0.05, "strong"),
            ("sonnet", 0.25, "medium"), ("haiku", 0.55, "fast"))
UNKNOWN = (0.25, "medium")
TIER_RANK = {"strong": 2, "medium": 1, "fast": 0}

WEIGHTS = {
    "model_switch": 0.05,        # also an explicit handoff (engine restarted on the same session)
    "quota_forced_fallback": 0.15,
    "fcc_backend_switch": 0.10,
    "implicit_handoff": 0.25,
    "retry_storm": 0.10,
    "tier_degradation_medium_fast": 0.30,
    "tier_degradation_strong_medium": 0.15,
    "context_truncation": 0.20,
    "provider_instability": 0.10,
    "long_session_turn": 0.02,   # per turn beyond LONG_SESSION
}
LONG_SESSION = 20
COMPACTION_DROP = 30.0  # context % fall between checks that reads as an auto-compact


def band_of(score: float) -> str:
    for name, upper in BANDS:
        if score < upper:
            return name
    return BANDS[-1][0]


def family(model: str | None) -> tuple[float, str]:
    name = (model or "").lower()
    for fragment, base, tier in FAMILIES:
        if fragment in name:
            return base, tier
    return UNKNOWN


def trust_for_score(score: float) -> str:
    """The profile a score earns. Thresholds sit on the band edges: STABLE is trusted,
    NOMINAL standard, ELEVATED and worse strict."""
    return "trusted" if score < 0.2 else "standard" if score < 0.5 else "strict"


@dataclass
class Entropy:
    model: str | None = None
    events: list[tuple[str, float, str]] = field(default_factory=list)
    turns: int = 0
    _ctx: float | None = None
    _limited: bool = False
    _errors: int = 0  # failed requests this turn
    _expect_switch: bool = False

    @property
    def baseline(self) -> float:
        return family(self.model)[0]

    @property
    def score(self) -> float:
        return round(min(1.0, self.baseline + sum(d for _, d, _ in self.events)), 3)

    @property
    def band(self) -> str:
        return band_of(self.score)

    @property
    def trust(self) -> str:
        return trust_for_score(self.score)

    def _add(self, kind: str, delta: float, note: str) -> str:
        self.events.append((kind, delta, note))
        return note

    def observe_model(self, model: str | None) -> None:
        """The session reported its real model: sets the baseline, costs nothing."""
        self.model = model

    def switch_model(self, model: str | None) -> str | None:
        """The Observer changed model mid-session. Returns a reason when it added entropy."""
        old, new = family(self.model)[1], family(model)[1]
        was = self.model
        self.model = model
        self._expect_switch = True
        if was is None and not self.turns:
            return None  # picked before the first turn: nothing to drift from
        if TIER_RANK[new] >= TIER_RANK[old]:
            return self._add("model_switch", WEIGHTS["model_switch"], f"model switch to {model or 'default'}")
        key = "tier_degradation_medium_fast" if new == "fast" else "tier_degradation_strong_medium"
        return self._add(key, WEIGHTS[key], f"{old} to {new} tier ({model or 'default'})")

    def handoff(self) -> str:
        """The engine restarted on the same session with the context carried (crew toggled,
        session resumed): an explicit handoff, the cheap kind."""
        return self._add("model_switch", WEIGHTS["model_switch"], "engine restarted with context carried")

    def organ_brief(self, organ: str, has_context: bool) -> str | None:
        """An organ was spawned. A thin brief (no files named, short) is the doc's
        implicit handoff: the organ starts with incomplete context."""
        if has_context:
            return None
        return self._add("implicit_handoff", WEIGHTS["implicit_handoff"], f"{organ} briefed without context")

    def model_used(self, model: str, previous: str | None) -> list[str]:
        """The lead's replies now come from `model`, `previous` before. A change nobody asked for
        is a fallback: quota_forced_fallback, plus the tier drop if it went down; between two
        non-Claude models it is an fcc backend switch. Returns the reasons that added entropy."""
        if previous is None or model == previous:
            return []
        if self._expect_switch:
            self._expect_switch = False
            return []
        reasons = []
        old, new = family(previous), family(model)
        claude = lambda m: "claude" in m.lower()  # noqa: E731
        if not claude(previous) and not claude(model):
            reasons.append(self._add("fcc_backend_switch", WEIGHTS["fcc_backend_switch"],
                                     f"backend switched to {model}"))
        else:
            reasons.append(self._add("quota_forced_fallback", WEIGHTS["quota_forced_fallback"],
                                     f"replies fell back from {previous} to {model}"))
        if TIER_RANK[new[1]] < TIER_RANK[old[1]]:
            key = "tier_degradation_medium_fast" if new[1] == "fast" else "tier_degradation_strong_medium"
            reasons.append(self._add(key, WEIGHTS[key], f"{old[1]} to {new[1]} tier ({model})"))
        self.model = model
        return reasons

    def request_failed(self) -> str | None:
        """An API error this turn. The first marks the provider unreliable, the third is a retry storm."""
        self._errors += 1
        if self._errors == 1:
            return self._add("provider_instability", WEIGHTS["provider_instability"], "request failed")
        if self._errors == 3:
            return self._add("retry_storm", WEIGHTS["retry_storm"], "retry storm (3 failures in one turn)")
        return None

    def turn_done(self) -> str | None:
        self._errors = 0
        self._expect_switch = False  # the Observer's switch has been through a turn
        self.turns += 1
        if self.turns > LONG_SESSION:
            return self._add("long_session_turn", WEIGHTS["long_session_turn"], f"long session ({self.turns} turns)")
        return None

    def context(self, pct: float) -> str | None:
        prev, self._ctx = self._ctx, pct
        if prev is not None and prev - pct >= COMPACTION_DROP:
            return self._add("context_truncation", WEIGHTS["context_truncation"], "context compacted")
        return None

    def rate_limited(self, rejected: bool) -> str | None:
        """Entering a rejected state costs once; staying in it does not."""
        was, self._limited = self._limited, rejected
        if rejected and not was:
            return self._add("provider_instability", WEIGHTS["provider_instability"], "provider rejected requests")
        return None
