"""Session entropy: how much of a model's capability this session has lost.
Implements RaBbLE-Grimoire/RaBbLE-sCoRE/RaBbLE-sCoRE-Entropy-Tracker.md (v0.4, "Capability Model")
for the UI engine (the doc's server/entropy.py does not exist yet).

  capability(category) = base(model, category) - 100 * (damage(category) + pressure(category))
  score                = 1 - capability(general) / 100        (the 0..1 entropy the entity's glitch reads)

  damage   = where information or provenance was lost. Transient faults (a failed request, a
             fallback, a switch) decay with clean turns; structural loss (compaction, a thin
             handoff, a tier drop) stays, and re-grounding (re-reading files, a passing test run)
             earns back at most half of it. Only a park, a fresh session from a handoff, recovers the rest.
  pressure = the live state of the session (context fill, a rate-limit warning, a long session).
             Rises and falls: a fresh session is clean, a nearly full one is not.

Categories (orchestra/capabilities.py): general, coding, tools, recall. Context fill and compaction hit
recall and coding harder than general; faults hit tools harder. The trust tier reads `coding`, because
trust gates Bash and edits.

Pure data: no SDK, no UI, deterministic.
"""

from dataclasses import dataclass, field

from . import capabilities as caps

# v0.5: entropy has two inputs. Capability is the model's (per category, benchmark-derived, fixed for the
# model); stability is the session's (damage + pressure + bloat, moves every turn, a park resets it).
# The entity draws capability as noise and resolution, stability as glitch and breaking (ui/face.py).
BLOAT_FROM, BLOAT_FULL, BLOAT_MAX = 40.0, 85.0, 0.30   # context fill that breaks the entity before it costs trust

# health bands on capability LOST in the session (damage + pressure), not on the model's strength:
# a fresh haiku is STABLE and strict; a compacted opus is ELEVATED and still capable
BANDS = (("STABLE", 0.10), ("NOMINAL", 0.25), ("ELEVATED", 0.40), ("DEGRADED", 0.60), ("UNSTABLE", 1.01))
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
}
STRUCTURAL = {"context_truncation", "implicit_handoff", "tier_degradation_medium_fast",
              "tier_degradation_strong_medium"}   # information lost: never decays
DECAY = 0.8                  # transient damage left after one clean turn
CREDIT_CAP = 0.5             # re-grounding earns back at most half the structural damage
GROUND = {"read": 0.02, "verify": 0.03}   # credit per distinct file re-read / passing test run

# which family an event belongs to, and how hard each family hits each category (default 1.0)
KIND_CLASS = {"context_truncation": "context", "implicit_handoff": "context", "provider_instability": "fault",
              "retry_storm": "fault", "quota_forced_fallback": "fault", "fcc_backend_switch": "fault"}
CATEGORY_WEIGHT = {"context": {"recall": 1.5, "coding": 1.2}, "fault": {"tools": 1.3}}

# pressure: live terms that recover (context fill, long session, rate-limit state)
CONTEXT_FROM, CONTEXT_FULL, CONTEXT_MAX = 50.0, 90.0, 0.25   # 0 below 50% full, ramping to +0.25 at 90%
LONG_SESSION, LONG_TURN, LONG_MAX = 20, 0.01, 0.10           # per turn beyond 20, capped
LIMIT_PRESSURE = {"warning": 0.05, "rejected": 0.10}
COMPACTION_DROP = 30.0  # context % fall between checks that reads as an auto-compact


BANDS_ORDER = {name: i for i, (name, _) in enumerate(BANDS)}


def band_of(lost: float) -> str:
    """Health band for capability lost (0..1 of the scale): damage + pressure."""
    for name, upper in BANDS:
        if lost < upper:
            return name
    return BANDS[-1][0]


def family(model: str | None) -> tuple[float, str]:
    """(baseline entropy, capability tier) for a model: the registry's `general`, as entropy."""
    c = caps.lookup(model)
    return round(1 - c.general / 100, 3), c.tier


def trust_for_score(score: float) -> str:
    """The profile an entropy score earns on the `general` scale: trusted >= 60 capability,
    standard >= 40, strict below."""
    return caps.trust_for_capability(100 * (1 - score))


def _w(cls: str, category: str) -> float:
    return CATEGORY_WEIGHT.get(cls, {}).get(category, 1.0)


@dataclass
class Entropy:
    model: str | None = None
    events: list[tuple[str, float, str]] = field(default_factory=list)
    turns: int = 0
    _ctx: float | None = None
    _limit: str = "allowed"
    _errors: int = 0  # failed requests this turn
    _expect_switch: bool = False
    _at: list[int] = field(default_factory=list)   # clean-turn count when each event happened
    _clean: int = 0
    _credit: float = 0.0
    _grounded: set = field(default_factory=set)

    # ── the model ───────────────────────────────────────────────────────────
    @property
    def cap(self) -> caps.Capability:
        return caps.lookup(self.model)

    @property
    def local(self) -> bool:
        return self.cap.local

    @property
    def baseline(self) -> float:
        return round(1 - self.cap.general / 100, 3)

    # ── damage and pressure, per category ───────────────────────────────────
    def structural(self) -> float:
        return sum(d for k, d, _ in self.events if k in STRUCTURAL)

    def _damage(self, category: str = "general") -> float:
        struct = trans = 0.0
        for (kind, d, _), at in zip(self.events, self._at):
            w = _w(KIND_CLASS.get(kind, "model"), category)
            if kind in STRUCTURAL:
                struct += d * w
            else:
                trans += d * w * DECAY ** max(0, self._clean - at)
        total = self.structural()
        if total:
            struct *= 1 - min(self._credit / total, CREDIT_CAP)
        return trans + struct

    def _pressure(self, category: str = "general") -> float:
        ctx = 0.0
        if self._ctx is not None and self._ctx > CONTEXT_FROM:
            ctx = CONTEXT_MAX * min(1.0, (self._ctx - CONTEXT_FROM) / (CONTEXT_FULL - CONTEXT_FROM))
        long = min(LONG_MAX, LONG_TURN * max(0, self.turns - LONG_SESSION))
        return ctx * _w("context", category) + long + LIMIT_PRESSURE.get(self._limit, 0.0) * _w("fault", category)

    @property
    def damage(self) -> float:
        return self._damage()

    @property
    def pressure(self) -> float:
        return self._pressure()

    @property
    def lost(self) -> float:
        return self.damage + self.pressure

    def capability(self, category: str = "general") -> int:
        raw = self.cap.get(category) - 100 * (self._damage(category) + self._pressure(category))
        return round(max(0.0, min(100.0, raw)))

    def capabilities(self) -> dict[str, int]:
        return {c: self.capability(c) for c in caps.CATEGORIES}

    # ── the two inputs (v0.5) ───────────────────────────────────────────────
    @property
    def bloat(self) -> float:
        """How full the window is, as a stability cost 0..BLOAT_MAX: starts at 40% context, full at 85%.
        Separate from pressure: a filling window tears the entity well before it costs capability or trust."""
        if self._ctx is None or self._ctx <= BLOAT_FROM:
            return 0.0
        return BLOAT_MAX * min(1.0, (self._ctx - BLOAT_FROM) / (BLOAT_FULL - BLOAT_FROM))

    @property
    def base_capability(self) -> int:
        """What the model can do, before the session touches it (0..100, `general`)."""
        return self.cap.general

    @property
    def stability(self) -> int:
        """How healthy the session is, 0..100: 100 minus damage, pressure and bloat. A fresh session is
        100 for every model, a small local one included."""
        return round(100 * max(0.0, min(1.0, 1 - self.lost - self.bloat)))

    @property
    def score(self) -> float:
        return round(max(0.0, min(1.0, 1 - self.capability("general") / 100)), 3)

    @property
    def band(self) -> str:
        return band_of(self.lost)

    @property
    def trust(self) -> str:
        return caps.trust_for_capability(self.capability("coding"))

    # ── events ──────────────────────────────────────────────────────────────
    def _add(self, kind: str, delta: float, note: str) -> str:
        self.events.append((kind, delta, note))
        self._at.append(self._clean)
        if kind in STRUCTURAL:
            self._grounded.clear()   # fresh loss: files can be re-read for credit again
        return note

    def reground(self, source: str, key: str | None = None) -> bool:
        """The session re-loaded what it had lost: a file re-read (`key` = its path, once each) or a
        passing test run. Earns back structural damage, never more than CREDIT_CAP of it."""
        total = self.structural()
        if total <= 0 or source not in GROUND:
            return False
        if key is not None:
            if key in self._grounded:
                return False
            self._grounded.add(key)
        cap = CREDIT_CAP * total
        if self._credit >= cap:
            return False
        self._credit = min(cap, self._credit + GROUND[source])
        return True

    def observe_model(self, model: str | None) -> None:
        """The session reported its real model: sets the baseline, costs nothing."""
        self.model = model

    def switch_model(self, model: str | None) -> str | None:
        """The Observer changed model mid-session. Returns a reason when it added entropy."""
        old, new = self.cap.tier, caps.lookup(model).tier
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
        session resumed), or a fresh session started from a parked handoff: the cheap kind."""
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
        old, new = caps.lookup(previous).tier, caps.lookup(model).tier
        claude = lambda m: "claude" in m.lower()  # noqa: E731
        if not claude(previous) and not claude(model):
            reasons.append(self._add("fcc_backend_switch", WEIGHTS["fcc_backend_switch"],
                                     f"backend switched to {model}"))
        else:
            reasons.append(self._add("quota_forced_fallback", WEIGHTS["quota_forced_fallback"],
                                     f"replies fell back from {previous} to {model}"))
        if TIER_RANK[new] < TIER_RANK[old]:
            key = "tier_degradation_medium_fast" if new == "fast" else "tier_degradation_strong_medium"
            reasons.append(self._add(key, WEIGHTS[key], f"{old} to {new} tier ({model})"))
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

    def _crossed(self, before: float, note: str) -> str | None:
        """Pressure moved the session into a worse band: say why, once per crossing."""
        return note if BANDS_ORDER[band_of(self.lost)] > BANDS_ORDER[band_of(before)] else None

    def turn_done(self) -> str | None:
        clean = self._errors == 0
        self._errors = 0
        self._expect_switch = False  # the Observer's switch has been through a turn
        before = self.lost
        self.turns += 1
        if clean:
            self._clean += 1         # a clean turn: transient faults fade
        return self._crossed(before, f"long session ({self.turns} turns)")

    def context(self, pct: float) -> str | None:
        before = self.lost
        prev, self._ctx = self._ctx, pct
        if prev is not None and prev - pct >= COMPACTION_DROP:
            return self._add("context_truncation", WEIGHTS["context_truncation"], "context compacted")
        return self._crossed(before, f"context {pct:.0f}% full")

    def rate_limited(self, status: str | bool) -> str | None:
        """A rejected state costs damage once on entry; the live state is pressure while it lasts."""
        if isinstance(status, bool):
            status = "rejected" if status else "allowed"
        before, was = self.lost, self._limit
        self._limit = status
        if status == "rejected" and was != "rejected":
            return self._add("provider_instability", WEIGHTS["provider_instability"], "provider rejected requests")
        return self._crossed(before, "rate limit warning")
