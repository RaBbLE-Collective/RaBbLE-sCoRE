//! The crew as the shell sees it: who is in it, who is working, who is waiting on the Observer, and
//! what each member did. Built only from the events already on the wire (every one carries its
//! `organ`), so it is a pure model: the panel and the viewer just read it.

use std::{
    collections::{HashMap, VecDeque},
    time::Instant,
};

use rabble_proto::OrganStatus;
use serde_json::{Map, Value};

pub const LEAD: &str = "rabble";
const LOG_CAP: usize = 500;
const HOP_CAP: usize = 40;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum State {
    Idle,
    Active,
    /// Blocked on a permission ask: the Observer is the one being waited for.
    Waiting,
    Done,
    Failed,
    Killed,
}

impl State {
    pub fn live(self) -> bool {
        matches!(self, State::Active | State::Waiting)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Say,
    Tool,
    Result { ok: bool },
    Note,
}

#[derive(Debug, Clone)]
pub struct Entry {
    pub kind: Kind,
    pub text: String,
}

#[derive(Debug, Clone)]
pub struct Member {
    pub name: String,
    pub state: State,
    pub brief: String,
    pub task_id: Option<String>,
    pub started: Option<Instant>,
    pub ended: Option<Instant>,
    pub last_tool: String,
    pub tokens: u64,
    pub tools: u64,
    pub log: VecDeque<Entry>,
    /// The last log entry is a Say still being streamed.
    open_say: bool,
}

impl Member {
    fn new(name: &str) -> Self {
        Member { name: name.to_owned(), state: State::Idle, brief: String::new(), task_id: None, started: None, ended: None, last_tool: String::new(), tokens: 0, tools: 0, log: VecDeque::new(), open_say: false }
    }

    fn push(&mut self, kind: Kind, text: String) {
        self.open_say = false;
        self.log.push_back(Entry { kind, text });
        while self.log.len() > LOG_CAP {
            self.log.pop_front();
        }
    }

    /// Seconds this member has been (or was) working.
    pub fn secs(&self) -> Option<u64> {
        let s = self.started?;
        Some(self.ended.unwrap_or_else(Instant::now).saturating_duration_since(s).as_secs())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Flow {
    /// The lead sent an organ off with a brief.
    Spawn,
    /// An organ handed its result back to the lead.
    Return,
    /// An organ needs the Observer.
    Ask,
    /// The Observer answered.
    Answer,
}

#[derive(Debug, Clone)]
pub struct Hop {
    pub from: String,
    pub to: String,
    pub kind: Flow,
    pub text: String,
}

#[derive(Debug, Default)]
pub struct Crew {
    pub members: Vec<Member>,
    pub hops: VecDeque<Hop>,
    tool_owner: HashMap<String, String>,
    asks: HashMap<String, String>,
}

/// The one input value worth showing beside a tool name.
fn gist(input: &Map<String, Value>) -> String {
    for k in ["command", "file_path", "path", "pattern", "url", "query", "description", "prompt"] {
        if let Some(v) = input.get(k).and_then(Value::as_str) {
            return v.lines().next().unwrap_or("").to_owned();
        }
    }
    String::new()
}

impl Crew {
    pub fn new() -> Self {
        let mut c = Crew::default();
        c.members.push(Member::new(LEAD));
        c
    }

    fn at(&mut self, name: &str) -> &mut Member {
        let name = if name.is_empty() { LEAD } else { name };
        if let Some(i) = self.members.iter().position(|m| m.name == name) {
            return &mut self.members[i];
        }
        self.members.push(Member::new(name));
        self.members.last_mut().unwrap()
    }

    pub fn get(&self, name: &str) -> Option<&Member> {
        self.members.iter().find(|m| m.name == name)
    }

    fn hop(&mut self, from: &str, to: &str, kind: Flow, text: &str) {
        self.hops.push_back(Hop { from: from.to_owned(), to: to.to_owned(), kind, text: text.lines().next().unwrap_or("").to_owned() });
        while self.hops.len() > HOP_CAP {
            self.hops.pop_front();
        }
    }

    /// Organs that have joined (everyone but the lead).
    pub fn organs(&self) -> impl Iterator<Item = &Member> {
        self.members.iter().filter(|m| m.name != LEAD)
    }

    pub fn active(&self) -> usize {
        self.organs().filter(|m| m.state.live()).count()
    }

    pub fn waiting(&self) -> usize {
        self.organs().filter(|m| m.state == State::Waiting).count()
    }

    /// A new session starts from nothing.
    pub fn clear(&mut self) {
        *self = Crew::new();
    }

    pub fn delta(&mut self, organ: &str, text: &str) {
        let m = self.at(organ);
        if m.open_say {
            if let Some(Entry { text: t, .. }) = m.log.back_mut() {
                t.push_str(text);
                return;
            }
        }
        m.push(Kind::Say, text.to_owned());
        m.open_say = true;
    }

    pub fn block(&mut self, organ: &str, text: &str) {
        let m = self.at(organ);
        if m.open_say {
            if let Some(e) = m.log.back_mut() {
                e.text = text.to_owned(); // the finished block replaces what streamed
                m.open_say = false;
                return;
            }
        }
        m.push(Kind::Say, text.to_owned());
    }

    pub fn note(&mut self, organ: &str, text: &str) {
        self.at(organ).push(Kind::Note, text.to_owned());
    }

    pub fn tool_call(&mut self, organ: &str, id: &str, name: &str, input: &Map<String, Value>) {
        let g = gist(input);
        self.tool_owner.insert(id.to_owned(), if organ.is_empty() { LEAD } else { organ }.to_owned());
        let m = self.at(organ);
        m.tools += 1;
        m.last_tool = if g.is_empty() { name.to_owned() } else { format!("{name} {g}") };
        let line = m.last_tool.clone();
        m.push(Kind::Tool, line);
    }

    pub fn tool_result(&mut self, id: &str, ok: bool, summary: &str) {
        if let Some(o) = self.tool_owner.remove(id) {
            self.at(&o).push(Kind::Result { ok }, summary.to_owned());
        }
    }

    pub fn started(&mut self, organ: &str, task_id: Option<String>, brief: &str) {
        let m = self.at(organ);
        m.state = State::Active;
        m.brief = brief.to_owned();
        m.task_id = task_id;
        m.started = Some(Instant::now());
        m.ended = None;
        m.tokens = 0;
        m.tools = 0;
        m.push(Kind::Note, format!("briefed: {brief}"));
        self.hop(LEAD, organ, Flow::Spawn, brief);
    }

    pub fn progress(&mut self, organ: &str, usage: Option<&Map<String, Value>>, last_tool: Option<&str>) {
        let m = self.at(organ);
        if let Some(u) = usage {
            m.tokens = u.get("total_tokens").and_then(Value::as_u64).unwrap_or(m.tokens);
            m.tools = u.get("tool_uses").and_then(Value::as_u64).unwrap_or(m.tools);
        }
        if let Some(t) = last_tool.filter(|t| !t.is_empty()) {
            if !m.last_tool.starts_with(t) {
                m.last_tool = t.to_owned();
            }
        }
    }

    pub fn done(&mut self, organ: &str, status: &OrganStatus, result: &str) {
        let (state, word) = match status {
            OrganStatus::Completed => (State::Done, "done"),
            OrganStatus::Failed => (State::Failed, "failed"),
            OrganStatus::Killed => (State::Killed, "stopped"),
            OrganStatus::Unknown => (State::Failed, "ended"),
        };
        let m = self.at(organ);
        m.state = state;
        m.ended = Some(Instant::now());
        m.push(Kind::Note, if result.is_empty() { word.to_owned() } else { format!("{word}: {result}") });
        let who = organ.to_owned();
        self.asks.retain(|_, o| *o != who);
        self.hop(organ, LEAD, Flow::Return, &if result.is_empty() { word.to_owned() } else { format!("{word}: {result}") });
    }

    pub fn ask(&mut self, organ: &str, id: &str, tool: &str) {
        let organ = if organ.is_empty() { LEAD } else { organ };
        self.asks.insert(id.to_owned(), organ.to_owned());
        let m = self.at(organ);
        if m.name != LEAD {
            m.state = State::Waiting;
        }
        m.push(Kind::Note, format!("waiting for you: {tool}"));
        self.hop(organ, "you", Flow::Ask, tool);
    }

    pub fn resolved(&mut self, id: &str, decision: &str) {
        let Some(o) = self.asks.remove(id) else { return };
        if !self.asks.values().any(|a| *a == o) {
            let m = self.at(&o);
            if m.state == State::Waiting {
                m.state = State::Active;
            }
        }
        self.hop("you", &o, Flow::Answer, decision);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn m(v: Value) -> Map<String, Value> {
        v.as_object().unwrap().clone()
    }

    #[test]
    fn the_lead_is_always_there_and_organs_join_when_briefed() {
        let mut c = Crew::new();
        assert_eq!(c.organs().count(), 0);
        c.started("gobble", Some("k1".into()), "map the repo");
        assert_eq!((c.organs().count(), c.active()), (1, 1));
        let g = c.get("gobble").unwrap();
        assert_eq!((g.state, g.task_id.as_deref(), g.brief.as_str()), (State::Active, Some("k1"), "map the repo"));
        assert_eq!((c.hops[0].from.as_str(), c.hops[0].to.as_str(), c.hops[0].kind), ("rabble", "gobble", Flow::Spawn));
    }

    #[test]
    fn streamed_text_joins_and_a_block_replaces_it() {
        let mut c = Crew::new();
        c.delta("gobble", "Hel");
        c.delta("gobble", "lo");
        assert_eq!(c.get("gobble").unwrap().log.len(), 1);
        c.block("gobble", "Hello there");
        let l = &c.get("gobble").unwrap().log;
        assert_eq!((l.len(), l[0].text.as_str()), (1, "Hello there"));
        c.delta("gobble", "next");
        assert_eq!(c.get("gobble").unwrap().log.len(), 2, "a closed block starts a new one");
    }

    #[test]
    fn tools_and_results_land_on_the_organ_that_called_them() {
        let mut c = Crew::new();
        c.tool_call("cobble", "t1", "Grep", &m(json!({"pattern": "fn main"})));
        c.tool_call("gobble", "t2", "Bash", &m(json!({"command": "ls\nsecond line"})));
        c.tool_result("t1", false, "no match");
        let cob = c.get("cobble").unwrap();
        assert_eq!(cob.last_tool, "Grep fn main");
        assert_eq!(cob.log.back().unwrap().kind, Kind::Result { ok: false });
        assert_eq!(c.get("gobble").unwrap().last_tool, "Bash ls", "only the first line of a command");
        assert_eq!(c.get("gobble").unwrap().log.len(), 1, "t1's result did not leak to gobble");
    }

    #[test]
    fn a_permission_ask_makes_the_organ_wait_until_it_is_answered() {
        let mut c = Crew::new();
        c.started("cobble", None, "build");
        c.started("nibble", None, "review");
        c.ask("cobble", "a1", "Edit");
        c.ask("cobble", "a2", "Bash");
        assert_eq!((c.get("cobble").unwrap().state, c.waiting(), c.active()), (State::Waiting, 1, 2));
        c.resolved("a1", "allow");
        assert_eq!(c.get("cobble").unwrap().state, State::Waiting, "a2 is still open");
        c.resolved("a2", "allow");
        assert_eq!(c.get("cobble").unwrap().state, State::Active);
        assert_eq!(c.hops.back().unwrap().kind, Flow::Answer);
    }

    #[test]
    fn done_ends_the_clock_and_hands_back() {
        let mut c = Crew::new();
        c.started("gobble", None, "scout");
        c.ask("gobble", "a1", "Read");
        c.done("gobble", &OrganStatus::Failed, "boom");
        let g = c.get("gobble").unwrap();
        assert_eq!(g.state, State::Failed);
        assert!(g.ended.is_some() && !g.state.live());
        assert_eq!((c.active(), c.hops.back().unwrap().kind), (0, Flow::Return));
        c.resolved("a1", "deny"); // a late resolve for a finished organ must not revive it
        assert_eq!(c.get("gobble").unwrap().state, State::Failed);
    }

    #[test]
    fn progress_takes_usage_and_the_logs_are_capped() {
        let mut c = Crew::new();
        c.started("gobble", None, "x");
        c.progress("gobble", Some(&m(json!({"total_tokens": 1234, "tool_uses": 5}))), Some("Read"));
        let g = c.get("gobble").unwrap();
        assert_eq!((g.tokens, g.tools, g.last_tool.as_str()), (1234, 5, "Read"));
        for i in 0..LOG_CAP + 50 {
            c.note("gobble", &format!("n{i}"));
        }
        assert_eq!(c.get("gobble").unwrap().log.len(), LOG_CAP);
    }
}
