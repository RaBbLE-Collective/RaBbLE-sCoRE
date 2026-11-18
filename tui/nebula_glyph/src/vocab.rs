//! The entity's vocabulary on the terminal surface: canonical ids only (state, cue, mood), plus the alias
//! table that lets callers still sending the TUI's old private names keep working.
//!
//! Source of the names: RaBbLE-Grimoire/registry/entity/RaBbLE-Entity-Registry-Schema.md section 3.
//! Source of `ALIASES`: RaBbLE-Grimoire/registry/entity/rabble.entity.json `aliases.score_tui`,
//! registry version 0.1.0 (copied by hand; `tests/vocab.rs` fails if the registry file drifts from it).

use crate::face::Expr;

/// Registry version the alias table and moves were taken from.
pub const REGISTRY_VERSION: &str = "0.1.0";

/// What RaBbLE is doing. `thinking` plays the moods `ponder`, then `process`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum State {
    Idle,
    Listening,
    Thinking,
    Speaking,
}

impl State {
    pub const ALL: [State; 4] = [State::Idle, State::Listening, State::Thinking, State::Speaking];
    pub fn name(self) -> &'static str {
        match self {
            State::Idle => "idle",
            State::Listening => "listening",
            State::Thinking => "thinking",
            State::Speaking => "speaking",
        }
    }
}

/// A one-shot trigger. The glyph gives `error` and `denied` an expression; the rest have no glyph form
/// yet and read as calm.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Cue {
    Poke,
    Shock,
    Denied,
    Error,
    Fresh,
    Dizzy,
    Touch,
}

impl Cue {
    pub const ALL: [Cue; 7] = [Cue::Poke, Cue::Shock, Cue::Denied, Cue::Error, Cue::Fresh, Cue::Dizzy, Cue::Touch];
    pub fn name(self) -> &'static str {
        match self {
            Cue::Poke => "poke",
            Cue::Shock => "shock",
            Cue::Denied => "denied",
            Cue::Error => "error",
            Cue::Fresh => "fresh",
            Cue::Dizzy => "dizzy",
            Cue::Touch => "touch",
        }
    }
}

/// A short overlay inside a state. `thinking` = `ponder` then `process`; `insight` is a brief peak.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Mood {
    Ponder,
    Process,
    Insight,
}

impl Mood {
    pub const ALL: [Mood; 3] = [Mood::Ponder, Mood::Process, Mood::Insight];
    pub fn name(self) -> &'static str {
        match self {
            Mood::Ponder => "ponder",
            Mood::Process => "process",
            Mood::Insight => "insight",
        }
    }
    /// Saccade speed (1.0 = the idle rate). `process` is the old "working" 2.5; the others are seeds.
    pub fn speed(self) -> f64 {
        match self {
            Mood::Ponder => 1.0,
            Mood::Process => 2.5,
            Mood::Insight => 3.0,
        }
    }
}

/// Seconds `thinking` spends in `ponder` before `process` (Pocket's 1.5 s split, schema section 3).
pub const PONDER_SECONDS: f64 = 1.5;
/// How long an `insight` peak lasts before the state's own mood resumes (seed).
pub const INSIGHT_SECONDS: f64 = 0.8;

/// What a caller can hand the driver: a state, or a cue.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Beat {
    State(State),
    Cue(Cue),
}

impl Beat {
    pub const IDLE: Beat = Beat::State(State::Idle);
    pub const LISTENING: Beat = Beat::State(State::Listening);
    pub const THINKING: Beat = Beat::State(State::Thinking);
    pub const SPEAKING: Beat = Beat::State(State::Speaking);
    pub const ERROR: Beat = Beat::Cue(Cue::Error);
    pub const DENIED: Beat = Beat::Cue(Cue::Denied);

    /// Canonical id: `idle`, `listening`, ... for states and `cue.error` for cues (the registry's dotted form).
    pub fn id(self) -> String {
        match self {
            Beat::State(s) => format!("state.{}", s.name()),
            Beat::Cue(c) => format!("cue.{}", c.name()),
        }
    }

    /// Parse a canonical id (`listening`, `state.listening`, `cue.error`) or a private TUI name through
    /// `ALIASES`. A bare canonical cue name (`error`) is accepted too, because the aliases make it one.
    /// Unknown names are `None`.
    pub fn parse(n: &str) -> Option<Beat> {
        let n = n.trim();
        let dotted = ALIASES.iter().find(|(p, _)| *p == n).map_or(n, |(_, c)| *c);
        if let Some(rest) = dotted.strip_prefix("state.") {
            return State::ALL.into_iter().find(|s| s.name() == rest).map(Beat::State);
        }
        if let Some(rest) = dotted.strip_prefix("cue.") {
            return Cue::ALL.into_iter().find(|c| c.name() == rest).map(Beat::Cue);
        }
        State::ALL.into_iter().find(|s| s.name() == dotted).map(Beat::State)
    }

    /// `parse`, with idle for anything unknown (what the old `Mood::from_name` did).
    pub fn from_name(n: &str) -> Beat {
        Beat::parse(n).unwrap_or(Beat::IDLE)
    }

    /// (expression pose, saccade speed). `thinking` reports its steady mood, `process`.
    pub fn info(self) -> (Expr, f64) {
        match self {
            Beat::State(State::Idle) => (Expr::Calm, 1.0),
            // the legacy listening and speaking poses are the golden oracle's (ui/face.py); see PARITY.md
            Beat::State(State::Listening) => (Expr::Listening, 1.0),
            Beat::State(State::Thinking) => (Expr::Calm, Mood::Process.speed()),
            Beat::State(State::Speaking) => (Expr::Speaking, 1.0),
            Beat::Cue(Cue::Error) => (Expr::Sad, 1.0),
            Beat::Cue(Cue::Denied) => (Expr::Angry, 1.0),
            Beat::Cue(_) => (Expr::Calm, 1.0),
        }
    }
}

/// Private TUI name -> canonical id. Copied from the registry (`aliases.score_tui`, version 0.1.0).
pub const ALIASES: [(&str, &str); 5] = [
    ("denied", "cue.denied"),
    ("error", "cue.error"),
    ("streaming", "state.speaking"),
    ("typing", "state.listening"),
    ("working", "state.thinking"),
];
