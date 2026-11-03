//! The engine seam, typed. NDJSON over a stream: one `Frame` per line down (`Down` plus the
//! `session` it belongs to), one `Up` per line up. Mirrors `orchestra/events.py` and
//! `orchestra/serve.py`; the contract test parses `tests/fixtures/orchestra/*.jsonl`
//! (recorded turns plus `all-events.jsonl`, one line per event kind).
//!
//! Forward compatible: an unknown message type becomes `Down::Unknown`, unknown fields are ignored,
//! unknown values of inner enums become their `Unknown` variant, and a known message whose body
//! fails to decode still yields a `Frame` (msg `Unknown`, `malformed` set) so a client can count
//! and log it instead of dropping it silently. Additive protocol changes never bump `PROTOCOL`;
//! `Down::Hello.features` says what the engine has, and a client refuses only below `MIN_PROTOCOL`.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

pub const PROTOCOL: u32 = 1;
/// The oldest engine `version` a client of this build works with.
pub const MIN_PROTOCOL: u32 = 1;

pub type Json = Map<String, Value>;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Decision {
    Allow,
    AllowSession,
    Deny,
    /// A value from a newer engine. Never sent.
    #[serde(other)]
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OrganStatus {
    Completed,
    Failed,
    Killed,
    #[serde(other)]
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LimitStatus {
    Allowed,
    Warning,
    Rejected,
    #[serde(other)]
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TurnOrigin {
    Observer,
    Background,
    #[serde(other)]
    Unknown,
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
    /// The Claude session id, what `create_session{resume}` takes.
    #[serde(default)]
    pub sdk_session: Option<String>,
    #[serde(default)]
    pub busy: bool,
}

/// Live state of a session, sent in `attached`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Snapshot {
    #[serde(default)]
    pub busy: bool,
    #[serde(default)]
    pub turn_owner: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub mode: Option<String>,
    #[serde(default)]
    pub trust: Option<String>,
    /// Running organs: `{organ, task_id, brief}`.
    #[serde(default)]
    pub organs: Vec<Json>,
    #[serde(default)]
    pub sdk_session_id: Option<String>,
}

/// Everything the engine sends: the server's own replies (lowercase `type`) and every
/// `orchestra.events` class (CapitalCase `type`, exactly as `Event.to_json()` writes them).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum Down {
    // server replies
    #[serde(rename = "hello")]
    Hello {
        version: u32,
        engine: String,
        user: String,
        #[serde(default)]
        features: Vec<String>,
    },
    #[serde(rename = "sessions")]
    Sessions { sessions: Vec<SessionInfo> },
    #[serde(rename = "session_created")]
    SessionCreated {},
    #[serde(rename = "attached")]
    /// The frame's `seq` is the last seq this attach replay reaches. `gap`: the client's `since_seq`
    /// fell off the ring, so a compact state (unsequenced) precedes the retained tail.
    Attached {
        role: String,
        #[serde(default)]
        gap: bool,
        #[serde(default)]
        snapshot: Option<Snapshot>,
    },
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
        mcp: Vec<Json>,
        plugins: Vec<String>,
        problems: Vec<String>,
    },
    // transcript
    TextDelta { organ: String, text: String },
    TextBlock { organ: String, text: String },
    Thinking { organ: String },
    ThinkingText { organ: String, text: String },
    Narration {
        organ: String,
        text: String,
        #[serde(default)]
        id: String,
    },
    ToolCall { organ: String, id: String, name: String, input: Json },
    ToolResult { organ: String, id: String, ok: bool, summary: String },
    UserSaid {
        sender: String,
        text: String,
        #[serde(default)]
        client: String,
    },
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
        #[serde(default = "yes")]
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

fn yes() -> bool {
    true
}

fn observer() -> TurnOrigin {
    TurnOrigin::Observer
}

/// One line from the engine: the message plus its envelope. `session` is `None` for engine-level
/// messages; `seq` is the per-session sequence number (events and `attached`); `id` echoes the
/// `id` of the command this answers. `id` is set ONLY for server replies (`hello`, `sessions`,
/// `session_created`, `attached`, `detached`, `session_closed`) and `Error`; for every event kind it is
/// `None`, because ToolCall, ToolResult, PermissionAsk and PermissionResolved carry their own body
/// field `id`, which belongs to `Down`, not to the envelope.
///
/// Resume bookkeeping: track `max(seq)` over the events you receive. `attached.seq` is the head and
/// arrives before the replay, so it is not a resume point. After a `gap` replay, dedupe asks by id.
#[derive(Debug, Clone, PartialEq)]
pub struct Frame {
    pub session: Option<String>,
    pub seq: Option<u64>,
    pub id: Option<String>,
    pub msg: Down,
    /// Set when the line was valid JSON but its body did not decode (`msg` is then `Unknown`).
    pub malformed: Option<String>,
}

impl Frame {
    pub fn parse(line: &str) -> Result<Frame, serde_json::Error> {
        let v: Value = serde_json::from_str(line)?;
        let session = v.get("session").and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_owned);
        let seq = v.get("seq").and_then(Value::as_u64);
        let is_reply = matches!(v.get("type").and_then(Value::as_str),
            Some("hello" | "sessions" | "session_created" | "attached" | "detached" | "session_closed" | "Error"));
        let id = v.get("id").and_then(Value::as_str).filter(|_| is_reply).map(str::to_owned);
        let (msg, malformed) = match serde_json::from_value::<Down>(v) {
            Ok(m) => (m, None),
            Err(e) => (Down::Unknown, Some(e.to_string())),
        };
        Ok(Frame { session, seq, id, msg, malformed })
    }

    pub fn to_value(&self) -> Value {
        let mut v = serde_json::to_value(&self.msg).expect("Down serialises");
        if let Some(o) = v.as_object_mut() {
            if let Some(s) = &self.session {
                o.insert("session".into(), Value::String(s.clone()));
            }
            if let Some(n) = self.seq {
                o.insert("seq".into(), Value::from(n));
            }
            if let Some(i) = &self.id {
                o.insert("id".into(), Value::String(i.clone()));
            }
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
    /// `since_seq`: the last seq the client already has (omit for everything the engine retains).
    Attach {
        session: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        since_seq: Option<u64>,
    },
    Detach { session: String },
    CloseSession { session: String },
    Grant { session: String, principal: String, role: String },
    Revoke { session: String, principal: String },
    Send { session: String, text: String },
    Interrupt { session: String },
    Answer {
        session: String,
        ask_id: String,
        decision: Decision,
        #[serde(default, skip_serializing_if = "String::is_empty")] reason: String,
        /// AskUserQuestion: question text → the chosen label (several joined by ", ").
        #[serde(default, skip_serializing_if = "Option::is_none")] answers: Option<std::collections::BTreeMap<String, String>>,
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

    /// The line with a request `id` the engine will echo on its reply or error.
    pub fn to_line_with_id(&self, id: &str) -> String {
        let mut v = serde_json::to_value(self).expect("Up serialises");
        v.as_object_mut().expect("Up is an object").insert("id".into(), Value::String(id.into()));
        let mut s = v.to_string();
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
        assert_eq!(kinds.len(), 22);
    }

    #[test]
    fn engine_replies_carry_their_session() {
        let f = Frame::parse(r#"{"type":"attached","session":"s1-1","role":"owner"}"#).unwrap();
        assert_eq!(f.session.as_deref(), Some("s1-1"));
        assert_eq!(f.msg, Down::Attached { role: "owner".into(), gap: false, snapshot: None });
        let h = Frame::parse(r#"{"type":"hello","version":1,"engine":"claude-code","user":"local:1000"}"#).unwrap();
        assert_eq!(h.session, None);
        let s = Frame::parse(r#"{"type":"sessions","sessions":[{"session":"s1","role":"owner","clients":2,"cwd":"/w","model":null,"provider":null}]}"#).unwrap();
        assert!(matches!(s.msg, Down::Sessions { ref sessions } if sessions[0].clients == 2));
    }

    #[test]
    fn unknown_types_and_fields_are_tolerated() {
        let f = Frame::parse(r#"{"type":"FutureKind","session":"s1","organ":"rabble","text":"Reading a file"}"#).unwrap();
        assert_eq!((f.session.as_deref(), f.msg), (Some("s1"), Down::Unknown));
        let t = Frame::parse(r#"{"type":"TextDelta","session":"s1","organ":"rabble","text":"hi","future_field":1}"#).unwrap();
        assert_eq!(t.msg, Down::TextDelta { organ: "rabble".into(), text: "hi".into() });
    }

    #[test]
    fn envelope_carries_seq_id_and_the_attach_snapshot() {
        let line = r#"{"type":"attached","session":"s1","seq":42,"id":"r3","role":"owner","gap":true,"snapshot":{"busy":true,"turn_owner":"local:1","model":"haiku","mode":null,"trust":"strict","organs":[{"organ":"gobble","task_id":"t1","brief":"x"}],"sdk_session_id":"abc","future":1}}"#;
        let f = Frame::parse(line).unwrap();
        assert_eq!((f.seq, f.id.as_deref(), f.malformed.as_ref()), (Some(42), Some("r3"), None));
        let Down::Attached { gap, snapshot: Some(s), .. } = &f.msg else { panic!("{:?}", f.msg) };
        assert!(*gap && s.busy && s.organs.len() == 1);
        assert_eq!((s.turn_owner.as_deref(), s.sdk_session_id.as_deref(), s.mode.as_deref()), (Some("local:1"), Some("abc"), None));
        // event body ids are not the envelope id
        let tc = Frame::parse(r#"{"type":"ToolCall","session":"s1","seq":8,"organ":"rabble","id":"tu1","name":"Bash","input":{}}"#).unwrap();
        assert_eq!(tc.id, None);
        assert!(matches!(tc.msg, Down::ToolCall { ref id, .. } if id == "tu1"));
        let pr = Frame::parse(r#"{"type":"PermissionResolved","session":"s1","seq":9,"id":"a1","decision":"deny","by":"x"}"#).unwrap();
        assert_eq!(pr.id, None);
        assert_eq!(pr.to_value()["id"], "a1");
        let err = Frame::parse(r#"{"type":"Error","id":"r9","text":"no"}"#).unwrap();
        assert_eq!(err.id.as_deref(), Some("r9"));
        let e = Frame::parse(r#"{"type":"TextDelta","session":"s1","seq":7,"organ":"rabble","text":"hi"}"#).unwrap();
        assert_eq!(e.seq, Some(7));
        assert_eq!(e.to_value()["seq"], 7);
    }

    #[test]
    fn hello_features_and_user_said_client() {
        let h = Frame::parse(r#"{"type":"hello","version":1,"engine":"e","user":"u","features":["seq","attach_since"]}"#).unwrap();
        assert!(matches!(h.msg, Down::Hello { ref features, .. } if features == &["seq", "attach_since"]));
        let old = Frame::parse(r#"{"type":"hello","version":1,"engine":"e","user":"u"}"#).unwrap();
        assert!(matches!(old.msg, Down::Hello { ref features, .. } if features.is_empty()));
        let u = Frame::parse(r#"{"type":"UserSaid","sender":"local:1","client":"gui","text":"hi"}"#).unwrap();
        assert_eq!(u.msg, Down::UserSaid { sender: "local:1".into(), text: "hi".into(), client: "gui".into() });
        let older = Frame::parse(r#"{"type":"UserSaid","sender":"local:1","text":"hi"}"#).unwrap();
        assert!(matches!(older.msg, Down::UserSaid { ref client, .. } if client.is_empty()));
    }

    #[test]
    fn newer_values_inside_known_messages_do_not_lose_the_frame() {
        let d = Frame::parse(r#"{"type":"PermissionResolved","session":"s","id":"a1","decision":"allow_always","by":"x"}"#).unwrap();
        assert_eq!(d.msg, Down::PermissionResolved { id: "a1".into(), decision: Decision::Unknown, by: "x".into() });
        let o = Frame::parse(r#"{"type":"OrganDone","organ":"g","task_id":"t","status":"paused"}"#).unwrap();
        assert!(matches!(o.msg, Down::OrganDone { status: OrganStatus::Unknown, .. }));
        let l = Frame::parse(r#"{"type":"RateLimited","status":"throttled"}"#).unwrap();
        assert!(matches!(l.msg, Down::RateLimited { status: LimitStatus::Unknown, .. }));
        let t = Frame::parse(r#"{"type":"TurnDone","stop_reason":"x","origin":"scheduled"}"#).unwrap();
        assert!(matches!(t.msg, Down::TurnDone { origin: TurnOrigin::Unknown, .. }));
        // mcp entries may carry non-string values
        let c = Frame::parse(r#"{"type":"Capabilities","slash_commands":[],"skills":[],"mcp":[{"name":"n","status":"ok","tools":3,"info":null}],"plugins":[],"problems":[]}"#).unwrap();
        assert!(matches!(c.msg, Down::Capabilities { ref mcp, .. } if mcp[0]["tools"] == 3));
        // OrganStarted.context defaults like Python (true)
        let s = Frame::parse(r#"{"type":"OrganStarted","organ":"g","tool_use_id":"u","task_id":null,"brief":"b"}"#).unwrap();
        assert!(matches!(s.msg, Down::OrganStarted { context: true, .. }));
    }

    #[test]
    fn a_body_that_does_not_decode_is_counted_not_dropped() {
        let f = Frame::parse(r#"{"type":"Usage","session":"s","seq":3,"tokens":"many"}"#).unwrap();
        assert_eq!((f.msg.clone(), f.session.as_deref(), f.seq), (Down::Unknown, Some("s"), Some(3)));
        assert!(f.malformed.is_some());
        assert!(Frame::parse("{not json").is_err());
    }

    #[test]
    fn up_attach_since_revoke_and_request_ids() {
        let a = Up::Attach { session: "s".into(), since_seq: Some(9) };
        assert_eq!(a.to_line(), "{\"type\":\"attach\",\"session\":\"s\",\"since_seq\":9}\n");
        assert_eq!(Up::Attach { session: "s".into(), since_seq: None }.to_line(), "{\"type\":\"attach\",\"session\":\"s\"}\n");
        assert_eq!(Up::Revoke { session: "s".into(), principal: "p".into() }.to_line(), "{\"type\":\"revoke\",\"session\":\"s\",\"principal\":\"p\"}\n");
        let v: Value = serde_json::from_str(&Up::ListSessions {}.to_line_with_id("r1")).unwrap();
        assert_eq!(v, serde_json::json!({"type": "list_sessions", "id": "r1"}));
        assert!(MIN_PROTOCOL <= PROTOCOL);
    }

    #[test]
    fn commands_serialise_the_way_serve_py_reads_them() {
        assert_eq!(Up::Hello { version: PROTOCOL, client: "rabble-tui".into() }.to_line(), "{\"type\":\"hello\",\"version\":1,\"client\":\"rabble-tui\"}\n");
        let create = Up::CreateSession { cwd: Some("/w".into()), resume: None, continue_last: Some(true), model: None, mode: None, crew: None, trust: None, provider: None };
        assert_eq!(create.to_line(), "{\"type\":\"create_session\",\"cwd\":\"/w\",\"continue\":true}\n");
        let a = Up::Answer { session: "s1".into(), ask_id: "a1".into(), decision: Decision::AllowSession, reason: String::new(), answers: None };
        assert_eq!(a.to_line(), "{\"type\":\"answer\",\"session\":\"s1\",\"ask_id\":\"a1\",\"decision\":\"allow_session\"}\n");
        assert_eq!(Up::ListSessions {}.to_line(), "{\"type\":\"list_sessions\"}\n");
        assert_eq!(serde_json::from_str::<Up>(&Up::SetTrust { session: "s".into(), profile: None }.to_line()).unwrap(), Up::SetTrust { session: "s".into(), profile: None });
    }
}
