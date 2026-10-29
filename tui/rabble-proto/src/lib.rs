//! The engine seam, typed. NDJSON over a stream: one `Frame` per line down (`Down` plus the
//! `session` it belongs to), one `Up` per line up. Mirrors `orchestra/events.py` and
//! `orchestra/serve.py`; the contract test parses `tests/fixtures/orchestra/*.jsonl`
//! (recorded turns plus `all-events.jsonl`, one line per event kind).
//!
//! Forward compatible: an unknown message type becomes `Down::Unknown` and unknown fields are ignored.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

pub const PROTOCOL: u32 = 1;

pub type Json = Map<String, Value>;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Decision {
    Allow,
    AllowSession,
    Deny,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OrganStatus {
    Completed,
    Failed,
    Killed,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LimitStatus {
    Allowed,
    Warning,
    Rejected,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TurnOrigin {
    Observer,
    Background,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionInfo {
    pub session: String,
    pub role: String,
    pub clients: u32,
    #[serde(default)]
    pub cwd: String,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub provider: Option<String>,
}

/// Everything the engine sends: the server's own replies (lowercase `type`) and every
/// `orchestra.events` class (CapitalCase `type`, exactly as `Event.to_json()` writes them).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum Down {
    // server replies
    #[serde(rename = "hello")]
    Hello { version: u32, engine: String, user: String },
    #[serde(rename = "sessions")]
    Sessions { sessions: Vec<SessionInfo> },
    #[serde(rename = "session_created")]
    SessionCreated {},
    #[serde(rename = "attached")]
    Attached { role: String },
    #[serde(rename = "detached")]
    Detached {},
    #[serde(rename = "session_closed")]
    SessionClosed {},

    // session
    SessionStarted { session_id: String, model: String, cwd: String },
    SessionResumed { session_id: String },
    Capabilities {
        slash_commands: Vec<String>,
        skills: Vec<String>,
        mcp: Vec<std::collections::BTreeMap<String, String>>,
        plugins: Vec<String>,
        problems: Vec<String>,
    },
    // transcript
    TextDelta { organ: String, text: String },
    TextBlock { organ: String, text: String },
    Thinking { organ: String },
    ToolCall { organ: String, id: String, name: String, input: Json },
    ToolResult { organ: String, id: String, ok: bool, summary: String },
    UserSaid { sender: String, text: String },
    // permissions
    PermissionAsk {
        id: String,
        organ: String,
        tool: String,
        input: Json,
        #[serde(default)]
        title: Option<String>,
        #[serde(default)]
        display_name: Option<String>,
        #[serde(default)]
        description: Option<String>,
        #[serde(default)]
        blocked_path: Option<String>,
        #[serde(default)]
        suggestions: Vec<Json>,
    },
    PermissionResolved {
        id: String,
        decision: Decision,
        #[serde(default)]
        by: String,
    },
    // organs
    OrganStarted {
        organ: String,
        tool_use_id: String,
        task_id: Option<String>,
        brief: String,
        #[serde(default)]
        context: bool,
    },
    OrganProgress {
        organ: String,
        task_id: String,
        #[serde(default)]
        usage: Option<Json>,
        #[serde(default)]
        last_tool_name: Option<String>,
    },
    OrganDone {
        organ: String,
        task_id: String,
        status: OrganStatus,
        #[serde(default)]
        result: String,
    },
    // meters
    Usage {
        tokens: u64,
        #[serde(default)]
        cost: Option<f64>,
        #[serde(default)]
        context_pct: Option<f64>,
    },
    RateLimited {
        status: LimitStatus,
        #[serde(default)]
        until: Option<f64>,
    },
    TurnDone {
        stop_reason: String,
        #[serde(default = "observer")]
        origin: TurnOrigin,
    },
    ModelUsed { model: String },
    EntropyChanged {
        score: f64,
        band: String,
        trust: String,
        #[serde(default)]
        reason: String,
        #[serde(default)]
        capability: i64,
        #[serde(default)]
        categories: std::collections::BTreeMap<String, i64>,
        #[serde(default)]
        local: bool,
        #[serde(default)]
        base: i64,
        #[serde(default)]
        stability: i64,
    },
    Error { text: String },

    /// A message type this build does not know (a newer engine). Ignore it, do not crash.
    #[serde(other)]
    Unknown,
}

fn observer() -> TurnOrigin {
    TurnOrigin::Observer
}

/// One line from the engine: the message and the session it is about (`None` = engine-level).
#[derive(Debug, Clone, PartialEq)]
pub struct Frame {
    pub session: Option<String>,
    pub msg: Down,
}

impl Frame {
    pub fn parse(line: &str) -> Result<Frame, serde_json::Error> {
        let v: Value = serde_json::from_str(line)?;
        let session = v.get("session").and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_owned);
        Ok(Frame { session, msg: serde_json::from_value(v)? })
    }

    pub fn to_value(&self) -> Value {
        let mut v = serde_json::to_value(&self.msg).expect("Down serialises");
        if let (Some(s), Some(o)) = (&self.session, v.as_object_mut()) {
            o.insert("session".into(), Value::String(s.clone()));
        }
        v
    }
}

/// Everything a client sends. Per-session commands carry `session`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Up {
    Hello { version: u32, client: String },
    ListSessions {},
    CreateSession {
        #[serde(skip_serializing_if = "Option::is_none")] cwd: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")] resume: Option<String>,
        #[serde(rename = "continue", skip_serializing_if = "Option::is_none")] continue_last: Option<bool>,
        #[serde(skip_serializing_if = "Option::is_none")] model: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")] mode: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")] crew: Option<bool>,
        #[serde(skip_serializing_if = "Option::is_none")] trust: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")] provider: Option<String>,
    },
    Attach { session: String },
    Detach { session: String },
    CloseSession { session: String },
    Grant { session: String, principal: String, role: String },
    Send { session: String, text: String },
    Interrupt { session: String },
    Answer {
        session: String,
        ask_id: String,
        decision: Decision,
        #[serde(default, skip_serializing_if = "String::is_empty")] reason: String,
    },
    SetModel { session: String, model: Option<String> },
    SetPermissionMode { session: String, mode: String },
    SetTrust { session: String, profile: Option<String> },
    StopTask { session: String, task_id: String },
}

impl Up {
    /// The NDJSON line to write (newline included).
    pub fn to_line(&self) -> String {
        let mut s = serde_json::to_string(self).expect("Up serialises");
        s.push('\n');
        s
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn fixtures() -> Vec<PathBuf> {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/orchestra");
        let mut v: Vec<_> = std::fs::read_dir(&dir).expect("fixtures dir").map(|e| e.unwrap().path())
            .filter(|p| p.extension().is_some_and(|x| x == "jsonl")).collect();
        v.sort();
        v
    }

    /// Every fixture line parses to a known variant. `all-events.jsonl` is the current contract and
    /// must also serialise back to the same JSON; older recordings may lack newer defaulted fields
    /// (that is what `#[serde(default)]` is for), so they are only required to parse.
    #[test]
    fn every_fixture_line_parses_and_the_contract_fixture_round_trips() {
        let files = fixtures();
        assert!(files.len() >= 3, "expected the recorded turns and all-events.jsonl");
        let mut n = 0;
        for f in files {
            for (i, line) in std::fs::read_to_string(&f).unwrap().lines().enumerate().filter(|(_, l)| !l.trim().is_empty()) {
                let frame = Frame::parse(line).unwrap_or_else(|e| panic!("{}:{}: {e}", f.display(), i + 1));
                assert_ne!(frame.msg, Down::Unknown, "{}:{} parsed as Unknown: {line}", f.display(), i + 1);
                if f.file_name().is_some_and(|n| n == "all-events.jsonl") {
                    let want: Value = serde_json::from_str(line).unwrap();
                    assert_eq!(frame.to_value(), want, "{}:{} did not round-trip", f.display(), i + 1);
                }
                n += 1;
            }
        }
        assert!(n >= 20);
    }

    #[test]
    fn all_events_fixture_covers_every_variant() {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/orchestra/all-events.jsonl");
        let kinds: std::collections::BTreeSet<String> = std::fs::read_to_string(path).unwrap().lines()
            .map(|l| serde_json::from_str::<Value>(l).unwrap()["type"].as_str().unwrap().to_owned()).collect();
        // Down's event variants: if this count changes, a variant was added without a fixture line
        assert_eq!(kinds.len(), 20);
    }

    #[test]
    fn engine_replies_carry_their_session() {
        let f = Frame::parse(r#"{"type":"attached","session":"s1-1","role":"owner"}"#).unwrap();
        assert_eq!(f.session.as_deref(), Some("s1-1"));
        assert_eq!(f.msg, Down::Attached { role: "owner".into() });
        let h = Frame::parse(r#"{"type":"hello","version":1,"engine":"claude-code","user":"local:1000"}"#).unwrap();
        assert_eq!(h.session, None);
        let s = Frame::parse(r#"{"type":"sessions","sessions":[{"session":"s1","role":"owner","clients":2,"cwd":"/w","model":null,"provider":null}]}"#).unwrap();
        assert!(matches!(s.msg, Down::Sessions { ref sessions } if sessions[0].clients == 2));
    }

    #[test]
    fn unknown_types_and_fields_are_tolerated() {
        let f = Frame::parse(r#"{"type":"Narration","session":"s1","organ":"rabble","text":"Reading a file"}"#).unwrap();
        assert_eq!((f.session.as_deref(), f.msg), (Some("s1"), Down::Unknown));
        let t = Frame::parse(r#"{"type":"TextDelta","session":"s1","organ":"rabble","text":"hi","future_field":1}"#).unwrap();
        assert_eq!(t.msg, Down::TextDelta { organ: "rabble".into(), text: "hi".into() });
    }

    #[test]
    fn commands_serialise_the_way_serve_py_reads_them() {
        assert_eq!(Up::Hello { version: PROTOCOL, client: "rabble-tui".into() }.to_line(), "{\"type\":\"hello\",\"version\":1,\"client\":\"rabble-tui\"}\n");
        let create = Up::CreateSession { cwd: Some("/w".into()), resume: None, continue_last: Some(true), model: None, mode: None, crew: None, trust: None, provider: None };
        assert_eq!(create.to_line(), "{\"type\":\"create_session\",\"cwd\":\"/w\",\"continue\":true}\n");
        let a = Up::Answer { session: "s1".into(), ask_id: "a1".into(), decision: Decision::AllowSession, reason: String::new() };
        assert_eq!(a.to_line(), "{\"type\":\"answer\",\"session\":\"s1\",\"ask_id\":\"a1\",\"decision\":\"allow_session\"}\n");
        assert_eq!(Up::ListSessions {}.to_line(), "{\"type\":\"list_sessions\"}\n");
        assert_eq!(serde_json::from_str::<Up>(&Up::SetTrust { session: "s".into(), profile: None }.to_line()).unwrap(), Up::SetTrust { session: "s".into(), profile: None });
    }
}
