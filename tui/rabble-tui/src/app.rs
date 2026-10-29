//! The shell's state machine. `App` owns what is on screen and turns terminal input and engine
//! frames into changes plus commands for the engine. It never blocks and never touches the socket
//! (`Handle::send` only queues), so the render loop in `run.rs` stays 1:1 with the keyboard.
//!
//! Engine contract (rabble-proto, serve.py docstring): after `hello` we create or attach one
//! session; we track max(seq) of EVENTS only (never `attached.seq`), and dedupe permission asks by
//! id so a replay after a gap cannot reopen an answered prompt.

use std::{
    collections::{HashSet, VecDeque},
    path::PathBuf,
    time::{Instant, SystemTime},
};

use crossterm::event::{Event, KeyCode, KeyEventKind, KeyModifiers, MouseEventKind};
use rabble_proto::{Decision, Down, Frame, LimitStatus, OrganStatus, SessionInfo, Snapshot, TurnOrigin, Up};
use ratatui::{layout::Rect, Frame as TermFrame};

use crate::{
    client::{Handle, Net},
    park,
    prefs::{self, Settings},
    sessions,
    theme::Theme,
    widgets::{
        composer::{Action, Composer},
        header::{draw_divider, HeaderState},
        modal::{Ask, Permission, Picked, Picker, Reply},
        transcript::{Transcript, View},
    },
};

pub const QUIT_WINDOW_MS: u128 = 2000;
pub const HELP: [(&str, &str); 13] = [
    ("/model", "pick the model (or /model opus|sonnet|haiku|fable|default|<id>)"),
    ("/crew", "organs on/off: /crew on|off (off = solo, no agents, cheapest)"),
    ("/trust", "auto-approve level: /trust strict|standard|trusted|auto (auto = earned from session entropy)"),
    ("/mode", "permission mode: /mode default|acceptEdits|plan|auto (Shift+Tab cycles)"),
    ("/status", "model, crew, trust and mode at a glance"),
    ("/park", "hand off to a fresh session: /park [now|continue] | auto|ask|off | continue on|off (parks itself at 80% context)"),
    ("/resume", "pick an earlier session"),
    ("/clear", "start a fresh session"),
    ("/copy", "copy the last reply to the clipboard (Ctrl-V pastes; Shift+drag selects text)"),
    ("/face", "show the full face"),
    ("/help", "this list"),
    ("/quit", "leave (Ctrl-C twice also works)"),
    ("Ctrl-O", "expand or collapse tool cards"),
];

#[derive(Debug)]
pub enum Msg {
    Input(Event, Instant),
    Net(Net),
}

#[derive(Debug, Clone, PartialEq)]
pub enum Effect {
    Copy(String),
}

#[derive(Debug, Clone)]
pub enum Start {
    New { resume: Option<String> },
    Attach(String),
}

pub struct Config {
    pub cwd: String,
    pub start: Start,
    pub client: String,
}

pub enum Overlay {
    None,
    Permission(Box<Permission>),
    Model(Picker),
    Sessions(Picker),
}

pub struct App {
    pub th: Theme,
    pub t: Transcript,
    pub composer: Composer,
    pub header: HeaderState,
    pub settings: Settings,
    pub overlay: Overlay,
    pub quit: bool,
    pub dirty: bool,
    pub effects: Vec<Effect>,
    pub malformed: u64,
    pub session: Option<String>,
    pub max_seq: u64,
    pub turn_open: bool,
    pub engine_ok: bool,
    cfg: Config,
    out: Handle,
    asks: VecDeque<Ask>,
    seen_asks: HashSet<String>,
    last_ctrl_c: Option<Instant>,
    sdk_session: Option<String>,
    started: bool,
    live: Vec<SessionInfo>,
    parking: Option<(PathBuf, SystemTime)>,
    park_pending: bool,
    park_warned: bool,
    park_blocked: bool,
    seed: Option<String>,
    continue_once: bool,
    entropy_trust: Option<String>,
    pub caps_line: String,
    pub focused: bool,
}

fn base_commands() -> Vec<String> {
    let mut v: Vec<String> = HELP.iter().filter(|(c, _)| c.starts_with('/')).map(|(c, _)| (*c).to_owned()).collect();
    v.sort();
    v
}

impl App {
    pub fn new(cfg: Config, th: Theme, settings: Settings, out: Handle) -> App {
        let header = HeaderState { model: settings.model.clone().unwrap_or_default(), mode: settings.mode.clone(), trust: settings.trust.clone().unwrap_or_default(), crew: settings.crew, cwd: cfg.cwd.clone(), connected: true, ..Default::default() };
        let composer = Composer::new(&cfg.cwd, base_commands());
        App {
            th, t: Transcript::new(), composer, header, settings, overlay: Overlay::None, quit: false, dirty: true, effects: vec![], malformed: 0,
            session: None, max_seq: 0, turn_open: false, engine_ok: true, cfg, out, asks: VecDeque::new(), seen_asks: HashSet::new(), last_ctrl_c: None,
            sdk_session: None, started: false, live: vec![], parking: None, park_pending: false, park_warned: false, park_blocked: false, seed: None,
            continue_once: false, entropy_trust: None, caps_line: String::new(), focused: true,
        }
    }

    pub fn notice(&mut self, text: &str, error: bool) {
        self.t.notice(text, error);
        self.dirty = true;
    }

    fn sid(&self) -> Option<String> {
        self.session.clone()
    }

    /// Send a per-session command, or say there is no session.
    fn to_session(&mut self, what: &str, f: impl FnOnce(String) -> Up) -> bool {
        match self.sid() {
            Some(s) => {
                self.out.send(&f(s));
                true
            }
            None => {
                self.notice(&format!("{what}: no engine session yet (/clear starts one)"), true);
                false
            }
        }
    }

    fn create(&mut self, resume: Option<String>, crew: bool) {
        self.out.request(
            &Up::CreateSession {
                cwd: Some(self.cfg.cwd.clone()), resume, continue_last: None, model: self.settings.model.clone(),
                mode: Some(self.settings.mode.clone()), crew: Some(crew), trust: self.settings.trust.clone(), provider: None,
            },
            "create",
        );
    }

    pub fn trust_profile(&self) -> String {
        self.settings.trust.clone().or_else(|| self.entropy_trust.clone()).unwrap_or_default()
    }

    fn sync_header(&mut self) {
        self.header.mode = self.settings.mode.clone();
        self.header.crew = self.settings.crew;
        self.header.trust = self.trust_profile();
    }

    // ---- engine ------------------------------------------------------------------------------
    pub fn on_net(&mut self, n: Net) {
        self.dirty = true;
        match n {
            Net::Frame(f) => self.on_frame(f),
            Net::Reply { label, frame } => match (label.as_str(), &frame.msg) {
                ("resume-list", Down::Sessions { sessions }) => {
                    self.live = sessions.clone();
                    self.open_session_picker();
                }
                _ => self.on_frame(frame),
            },
            Net::Malformed { why, line } => {
                self.malformed += 1;
                if self.malformed == 1 || self.malformed % 20 == 0 {
                    self.notice(&format!("engine sent a line I could not read ({} so far): {why}: {line}", self.malformed), true);
                }
            }
            Net::Closed(why) => {
                self.engine_ok = false;
                self.header.connected = false;
                self.turn_open = false;
                self.t.end_turn();
                self.notice(&format!("engine connection lost: {why}. Restart rabble to reconnect; the session keeps running in the engine"), true);
            }
        }
    }

    fn is_reply_kind(m: &Down) -> bool {
        matches!(m, Down::Hello { .. } | Down::Sessions { .. } | Down::SessionCreated {} | Down::Attached { .. } | Down::Detached {} | Down::SessionClosed {})
    }

    pub fn on_frame(&mut self, f: Frame) {
        self.dirty = true;
        match (&f.msg, &f.session) {
            (Down::Hello { version, .. }, _) => {
                if *version < rabble_proto::MIN_PROTOCOL {
                    return self.notice(&format!("engine speaks protocol {version}, this build needs {}", rabble_proto::MIN_PROTOCOL), true);
                }
                if !std::mem::replace(&mut self.started, true) {
                    match self.cfg.start.clone() {
                        Start::New { resume } => self.create(resume, self.settings.crew),
                        Start::Attach(id) => {
                            self.session = Some(id.clone());
                            self.out.request(&Up::Attach { session: id, since_seq: None }, "attach");
                        }
                    }
                }
                return;
            }
            (Down::Sessions { sessions }, _) => {
                self.live = sessions.clone();
                return;
            }
            (Down::Error { text }, _) => {
                self.t.end_turn();
                self.notice(text, true);
                return;
            }
            (Down::SessionCreated {}, Some(s)) => {
                self.session = Some(s.clone());
                self.max_seq = 0;
                return;
            }
            (_, Some(s)) if self.session.as_deref() != Some(s.as_str()) => return, // some other session
            _ => {}
        }
        if !Self::is_reply_kind(&f.msg) {
            if let Some(seq) = f.seq {
                self.max_seq = self.max_seq.max(seq);
            }
        }
        match f.msg {
            Down::Attached { role, gap, snapshot } => {
                if role == "observer" {
                    self.notice("attached as observer: you can read but not send", false);
                }
                if gap {
                    self.notice("this view fell behind the engine's buffer; earlier output is summarised", false);
                }
                if let Some(s) = snapshot {
                    self.apply_snapshot(s);
                }
            }
            Down::SessionClosed {} => {
                self.session = None;
                self.turn_open = false;
                self.notice("session closed", false);
            }
            Down::Detached {} => {}
            Down::SessionStarted { session_id, model, .. } => {
                self.sdk_session = Some(session_id);
                if !model.is_empty() {
                    self.header.model = model;
                }
                if let Some(seed) = self.seed.take() {
                    self.turn_open = true;
                    self.to_session("send", |s| Up::Send { session: s, text: seed });
                }
            }
            Down::SessionResumed { session_id } => {
                self.notice(&format!("resumed session {}", session_id.chars().take(8).collect::<String>()), false);
                self.sdk_session = Some(session_id);
            }
            Down::Capabilities { slash_commands, skills, mcp, problems, .. } => {
                let mut cmds = base_commands();
                cmds.extend(slash_commands.iter().map(|c| format!("/{}", c.trim_start_matches('/'))));
                cmds.sort();
                cmds.dedup();
                self.composer.commands = cmds;
                let up = mcp.iter().filter(|m| m.get("status").and_then(|s| s.as_str()) == Some("connected")).count();
                self.caps_line = format!(" · {} commands · {} skills{}", slash_commands.len(), skills.len(), if mcp.is_empty() { String::new() } else { format!(" · mcp {up}/{}", mcp.len()) });
                for p in problems {
                    self.notice(&p, true);
                }
            }
            Down::TextDelta { organ, text } => self.t.delta(&organ, &text),
            Down::TextBlock { organ, text } => self.t.block(&organ, &text),
            Down::Thinking { .. } => self.t.thinking(),
            Down::ToolCall { organ, id, name, input } => self.t.tool_call(&organ, &id, &name, &input),
            Down::ToolResult { id, ok, summary, .. } => self.t.tool_result(&id, ok, &summary),
            Down::UserSaid { text, client, .. } => {
                if client == self.cfg.client {
                    self.t.user(&text); // our own message: jump to the bottom
                    self.t.thinking();
                } else {
                    // somebody else's, or history replayed on attach: do not yank the reader
                    let follow = self.t.follow;
                    self.t.user(&text);
                    self.t.follow = follow;
                }
            }
            Down::PermissionAsk { id, organ, tool, input, title, description, blocked_path, .. } => {
                if self.seen_asks.insert(id.clone()) {
                    self.asks.push_back(Ask { id, organ, tool, input, title, description, blocked_path });
                    self.next_ask();
                }
            }
            Down::PermissionResolved { id, decision, by } => {
                self.asks.retain(|a| a.id != id);
                self.seen_asks.insert(id.clone());
                if matches!(&self.overlay, Overlay::Permission(p) if p.ask.id == id) {
                    self.overlay = Overlay::None;
                    if decision != Decision::Unknown {
                        let d = if decision == Decision::Deny { "denied" } else { "allowed" };
                        self.notice(&format!("permission {d} by {}", if by.is_empty() { "another client" } else { &by }), decision == Decision::Deny);
                    }
                    self.next_ask();
                }
            }
            Down::OrganStarted { organ, brief, .. } => self.notice(&format!("{organ} started: {brief}"), false),
            Down::OrganDone { organ, status, .. } => {
                let (word, err) = match status {
                    OrganStatus::Completed => ("completed", false),
                    OrganStatus::Failed => ("failed", true),
                    OrganStatus::Killed => ("killed", true),
                    OrganStatus::Unknown => ("ended", true),
                };
                self.notice(&format!("{organ} {word}"), err);
            }
            Down::OrganProgress { .. } => {}
            Down::ModelUsed { model } => self.header.model = model,
            Down::EntropyChanged { score, band, trust, reason, .. } => {
                self.entropy_trust = Some(trust.clone());
                self.header.entropy = Some((score, band.clone()));
                self.sync_header();
                if !reason.is_empty() && !matches!(band.as_str(), "STABLE" | "NOMINAL") {
                    let bad = matches!(band.as_str(), "DEGRADED" | "UNSTABLE");
                    self.notice(&format!("entropy {} ({score:.2}): {reason}. Verify critical decisions. trust {trust}", band.to_lowercase()), bad);
                }
            }
            Down::Usage { cost, context_pct, .. } => {
                if cost.is_some() {
                    self.header.cost = cost;
                }
                if context_pct.is_some() {
                    self.header.context_pct = context_pct;
                }
                if let Some(p) = context_pct {
                    self.check_park(p);
                }
            }
            Down::RateLimited { status, .. } => {
                let name = match status {
                    LimitStatus::Allowed => None,
                    LimitStatus::Warning => Some("warning"),
                    LimitStatus::Rejected => Some("rejected"),
                    LimitStatus::Unknown => Some("limited"),
                };
                self.header.limit = name.map(str::to_owned);
                if let Some(n) = name {
                    self.notice(&format!("rate limit {n}"), n == "rejected");
                }
            }
            Down::TurnDone { origin, .. } => {
                self.t.end_turn();
                if origin == TurnOrigin::Observer {
                    self.turn_open = false;
                    if self.parking.is_some() {
                        self.finish_park();
                    } else if self.park_pending {
                        self.begin_park(None);
                    }
                } else {
                    self.notice("background turn finished", false);
                }
            }
            _ => {}
        }
    }

    fn apply_snapshot(&mut self, s: Snapshot) {
        self.turn_open = s.busy;
        if let Some(m) = s.model {
            self.header.model = m;
        }
        if let Some(m) = s.mode.filter(|m| prefs::MODES.contains(&m.as_str())) {
            self.settings.mode = m;
        }
        if let Some(t) = s.trust {
            self.entropy_trust.get_or_insert(t);
        }
        if s.sdk_session_id.is_some() {
            self.sdk_session = s.sdk_session_id;
        }
        self.sync_header();
    }

    // ---- permissions -------------------------------------------------------------------------
    fn next_ask(&mut self) {
        if !matches!(self.overlay, Overlay::None) {
            return;
        }
        if let Some(a) = self.asks.pop_front() {
            self.overlay = Overlay::Permission(Box::new(Permission::new(a)));
        }
    }

    fn answer(&mut self, r: Reply) {
        let Overlay::Permission(p) = std::mem::replace(&mut self.overlay, Overlay::None) else { return };
        let (id, decision) = (p.ask.id.clone(), r.decision.clone());
        self.to_session("answer", |s| Up::Answer { session: s, ask_id: id, decision: r.decision, reason: r.reason });
        if decision == Decision::Deny {
            self.notice(&format!("denied {}", p.ask.tool), true);
        }
        self.next_ask();
    }

    // ---- parking -----------------------------------------------------------------------------
    fn check_park(&mut self, pct: f64) {
        if self.settings.park == "off" || self.parking.is_some() || self.park_blocked {
            return;
        }
        if pct >= park::park_at() {
            if self.settings.park == "ask" {
                if !self.park_warned {
                    self.park_warned = true;
                    self.notice(&format!("context {pct:.0}%: /park hands off to a fresh session"), false);
                }
            } else if self.turn_open {
                self.park_pending = true;
            } else {
                self.begin_park(Some(pct));
            }
        } else if pct >= park::warn_at() && !self.park_warned {
            self.park_warned = true;
            self.notice(&format!("context {pct:.0}%: parking at {:.0}% (handoff, then a fresh session; /park off to stop)", park::park_at()), false);
        }
    }

    fn begin_park(&mut self, pct: Option<f64>) {
        let pct = pct.or(self.header.context_pct).unwrap_or(0.0);
        self.park_pending = false;
        let path = park::handoff_path(std::path::Path::new(&self.cfg.cwd), self.sdk_session.as_deref());
        if let Some(dir) = path.parent() {
            if let Err(e) = std::fs::create_dir_all(dir) {
                self.park_blocked = true;
                return self.notice(&format!("park failed: cannot use {}: {e}", dir.display()), true);
            }
        }
        self.notice(&format!("context {pct:.0}%: parking. Handoff and session log first, then a fresh session"), false);
        self.turn_open = true;
        let prompt = park::park_prompt(&path, pct);
        self.parking = Some((path, SystemTime::now()));
        if self.to_session("park", |s| Up::Send { session: s, text: prompt }) {
            self.t.thinking();
        } else {
            self.parking = None;
            self.park_blocked = true;
        }
    }

    fn finish_park(&mut self) {
        let Some((path, since)) = self.parking.take() else { return };
        if let Some(why) = park::check_handoff(&path, since) {
            self.park_blocked = true;
            return self.notice(&format!("park failed: {why}. Staying in this session; /park tries again"), true);
        }
        let keep = self.settings.park_continue || std::mem::take(&mut self.continue_once);
        self.seed = Some(park::fresh_prompt(&path, keep));
        self.restart(None, true);
        self.notice(&format!("parked; fresh session starting from {}{}", path.display(), if keep { " and carrying on" } else { " and waiting for you" }), false);
    }

    /// Close the current engine session and start another; the old one's events are ignored.
    fn restart(&mut self, resume: Option<String>, clear: bool) {
        if let Some(old) = self.session.take() {
            self.out.send(&Up::CloseSession { session: old });
        }
        self.asks.clear();
        self.overlay = Overlay::None;
        self.turn_open = false;
        self.max_seq = 0;
        self.park_pending = false;
        self.park_warned = false;
        if clear {
            self.t.clear();
            self.header.context_pct = None;
            self.header.entropy = None;
            self.entropy_trust = None;
        }
        self.create(resume, self.settings.crew);
    }

    // ---- input -------------------------------------------------------------------------------
    pub fn on_msg(&mut self, m: Msg) {
        match m {
            Msg::Input(e, _) => self.on_input(e),
            Msg::Net(n) => self.on_net(n),
        }
    }

    pub fn on_input(&mut self, e: Event) {
        self.dirty = true;
        match e {
            Event::Resize(..) => {}
            Event::FocusLost => {
                self.focused = false;
                self.t.blur();
            }
            Event::FocusGained => {
                self.focused = true;
                self.t.refocus();
            }
            Event::Paste(s) => match &mut self.overlay {
                Overlay::Permission(p) => {
                    if let Some(r) = &mut p.reason {
                        r.push_str(&s.replace(['\r', '\n'], " "));
                    }
                }
                Overlay::None => self.composer.insert_str(&s),
                _ => {}
            },
            Event::Mouse(m) => {
                if matches!(self.overlay, Overlay::None) {
                    match m.kind {
                        MouseEventKind::ScrollDown => self.t.wheel(true),
                        MouseEventKind::ScrollUp => self.t.wheel(false),
                        _ => {}
                    }
                }
            }
            Event::Key(k) if k.kind != KeyEventKind::Release => self.on_key(k),
            _ => {}
        }
    }

    fn on_key(&mut self, k: crossterm::event::KeyEvent) {
        match &mut self.overlay {
            Overlay::Permission(p) => {
                if let Some(r) = p.key(k) {
                    self.answer(r);
                }
                return;
            }
            Overlay::Model(p) => {
                match p.key(k) {
                    Picked::Close => self.overlay = Overlay::None,
                    Picked::Choose(v) => {
                        self.overlay = Overlay::None;
                        self.set_model(if v.is_empty() { None } else { Some(v) });
                    }
                    Picked::None => {}
                }
                return;
            }
            Overlay::Sessions(p) => {
                match p.key(k) {
                    Picked::Close => self.overlay = Overlay::None,
                    Picked::Choose(v) => {
                        self.overlay = Overlay::None;
                        self.resume_choice(&v);
                    }
                    Picked::None => {}
                }
                return;
            }
            Overlay::None => {}
        }
        let (ctrl, shift) = (k.modifiers.contains(KeyModifiers::CONTROL), k.modifiers.contains(KeyModifiers::SHIFT));
        match k.code {
            KeyCode::Char('o') if ctrl => self.t.toggle_cards(),
            KeyCode::PageUp => self.t.page(false),
            KeyCode::PageDown => self.t.page(true),
            KeyCode::Home if ctrl => self.t.jump_top(),
            KeyCode::End if ctrl => self.t.jump_bottom(),
            KeyCode::Up if shift => self.t.scroll_by(-1),
            KeyCode::Down if shift => self.t.scroll_by(1),
            _ => match self.composer.key(k) {
                Action::None => {}
                Action::Submit(text) => self.submit(text),
                Action::Interrupt => {
                    self.asks.clear();
                    if self.sid().is_some() {
                        self.to_session("interrupt", |s| Up::Interrupt { session: s });
                    }
                    self.t.end_turn();
                }
                Action::CycleMode => {
                    let i = prefs::MODES.iter().position(|m| *m == self.settings.mode).unwrap_or(0);
                    self.set_mode(prefs::MODES[(i + 1) % prefs::MODES.len()]);
                }
                Action::CtrlC => {
                    let now = Instant::now();
                    if self.last_ctrl_c.is_some_and(|t| now.duration_since(t).as_millis() <= QUIT_WINDOW_MS) {
                        self.quit = true;
                    } else {
                        self.last_ctrl_c = Some(now);
                        self.notice("press Ctrl-C again to quit", false);
                    }
                }
                Action::CtrlD => self.quit = true,
                Action::Paste => {
                    let t = crate::util::system_clipboard();
                    if !t.is_empty() {
                        self.composer.insert_str(&t);
                    }
                }
            },
        }
    }

    pub fn submit(&mut self, text: String) {
        self.t.drop_transient();
        let word = text.split_whitespace().next().unwrap_or("").to_owned();
        if HELP.iter().any(|(c, _)| *c == word && c.starts_with('/')) {
            let arg = text[word.len()..].trim().to_owned();
            return self.command(&word, &arg);
        }
        if self.parking.is_some() {
            return self.notice("parking: the handoff is being written, then a fresh session starts", true);
        }
        self.turn_open = true;
        if !self.to_session("send", |s| Up::Send { session: s, text }) {
            self.turn_open = false;
        } else if !self.engine_ok {
            self.notice("engine connection is gone; this message was not delivered", true);
        }
    }

    fn command(&mut self, word: &str, arg: &str) {
        match word {
            "/quit" => self.quit = true,
            "/model" => {
                if arg.is_empty() {
                    let items = prefs::MODELS.iter().map(|(l, v)| (format!("{}{l}{}", if *v == self.settings.model.as_deref() { "* " } else { "  " }, v.filter(|v| v != l).map(|v| format!("  {v}")).unwrap_or_default()), v.unwrap_or("").to_owned())).collect();
                    self.overlay = Overlay::Model(Picker::new("Model", items, false, "", Some(self.settings.model.as_deref().unwrap_or(""))));
                } else {
                    self.set_model(prefs::resolve_model(arg));
                }
            }
            "/mode" => {
                if prefs::MODES.contains(&arg) {
                    self.set_mode(arg);
                } else {
                    self.notice(&format!("usage: /mode {}", prefs::MODES.join("|")), true);
                }
            }
            "/trust" => {
                let a = arg.to_lowercase();
                if !prefs::TRUST.contains(&a.as_str()) && a != "auto" {
                    return self.notice(&format!("trust is {}; usage: /trust {}|auto", self.trust_profile(), prefs::TRUST.join("|")), !a.is_empty());
                }
                self.settings.trust = (a != "auto").then(|| a.clone());
                self.settings.save();
                let p = self.settings.trust.clone();
                self.to_session("trust", |s| Up::SetTrust { session: s, profile: p });
                self.sync_header();
                self.notice(&format!("trust {}{}", self.trust_profile(), if a == "auto" { " (from entropy)" } else { "" }), false);
            }
            "/crew" => {
                let a = arg.to_lowercase();
                if a != "on" && a != "off" {
                    return self.notice(&format!("crew is {}; usage: /crew on|off", if self.settings.crew { "on" } else { "off" }), !a.is_empty());
                }
                let want = a == "on";
                if want == self.settings.crew {
                    return self.notice(&format!("crew already {a}"), false);
                }
                self.settings.crew = want;
                self.settings.save();
                self.sync_header();
                let Some(sdk) = self.sdk_session.clone() else { return self.notice("crew changes apply to the next session (no session id yet)", false) };
                // agents live in the engine's start options: restart on the same Claude session
                self.restart(Some(sdk), false);
                self.notice(&format!("crew {a}{}", if want { " · organs cost a full context each, use for parallel work" } else { " · solo" }), false);
            }
            "/park" => self.park_command(arg),
            "/status" => {
                let s = self.status();
                self.notice(&s, false);
            }
            "/help" => {
                let mut s = HELP.iter().map(|(c, d)| format!("{c:8} {d}")).collect::<Vec<_>>().join("\n");
                s.push_str("\nOther /commands (/compact, /init, skills...) go to Claude Code as typed.");
                self.notice(&s, false);
            }
            "/copy" => match self.t.last_reply().map(str::to_owned) {
                Some(t) if !t.is_empty() => {
                    let n = t.chars().count();
                    self.effects.push(Effect::Copy(t));
                    self.notice(&format!("copied last reply ({n} chars)"), false);
                }
                _ => self.notice("nothing to copy (last reply)", true),
            },
            "/face" => self.notice("the full face is not wired into the Rust shell yet (rabble-face lands separately)", false),
            "/clear" => {
                self.restart(None, true);
                self.notice("new session", false);
            }
            "/resume" => {
                self.out.request(&Up::ListSessions {}, "resume-list");
            }
            _ => {}
        }
    }

    fn open_session_picker(&mut self) {
        let home = PathBuf::from(std::env::var_os("HOME").unwrap_or_default());
        let now = SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs());
        let mut items: Vec<(String, String)> = self
            .live
            .iter()
            .filter(|s| Some(&s.session) != self.session.as_ref() && s.cwd == self.cfg.cwd)
            .map(|s| (format!("running {} · {} client(s){}", s.session, s.clients, if s.busy { " · busy" } else { "" }), format!("attach:{}", s.session)))
            .collect();
        let running: HashSet<_> = self.live.iter().filter_map(|s| s.sdk_session.clone()).collect();
        items.extend(
            sessions::recent(&home, std::path::Path::new(&self.cfg.cwd), 20).into_iter().filter(|r| !running.contains(&r.id)).map(|r| (format!("{:<70} {}", r.title, sessions::ago(r.updated, now)), format!("resume:{}", r.id))),
        );
        self.overlay = Overlay::Sessions(Picker::new("Resume a session", items, true, "No earlier sessions here", None));
    }

    fn resume_choice(&mut self, v: &str) {
        if let Some(id) = v.strip_prefix("attach:") {
            if let Some(old) = self.session.take() {
                self.out.send(&Up::Detach { session: old });
            }
            self.t.clear();
            self.max_seq = 0;
            self.session = Some(id.to_owned());
            self.out.request(&Up::Attach { session: id.to_owned(), since_seq: None }, "attach");
            self.notice(&format!("attaching to {id}"), false);
        } else if let Some(id) = v.strip_prefix("resume:") {
            self.restart(Some(id.to_owned()), true);
            self.notice(&format!("resuming {}", id.chars().take(8).collect::<String>()), false);
        }
    }

    fn set_model(&mut self, model: Option<String>) {
        let m = model.clone();
        if !self.to_session("model", |s| Up::SetModel { session: s, model: m }) {
            return;
        }
        self.settings.model = model.clone();
        self.settings.save();
        self.header.model = model.clone().unwrap_or_else(|| "default".into());
        self.sync_header();
        self.notice(&format!("model {} · trust {}", model.as_deref().unwrap_or("default"), self.trust_profile()), false);
    }

    fn set_mode(&mut self, mode: &str) {
        let m = mode.to_owned();
        if !self.to_session("mode", |s| Up::SetPermissionMode { session: s, mode: m }) {
            return;
        }
        self.settings.mode = mode.to_owned();
        self.settings.save();
        self.sync_header();
    }

    fn park_command(&mut self, arg: &str) {
        let w: Vec<String> = arg.to_lowercase().split_whitespace().map(str::to_owned).collect();
        let w: Vec<&str> = w.iter().map(String::as_str).collect();
        match w.as_slice() {
            [] | ["status"] => {
                let s = format!("park {} at {:.0}% context, new session {}; /park now | /park continue | /park auto|ask|off | /park continue on|off", self.settings.park, park::park_at(), if self.settings.park_continue { "carries on" } else { "waits for you" });
                self.notice(&s, false);
            }
            [m] if prefs::PARK_MODES.contains(m) => {
                self.settings.park = (*m).to_owned();
                self.settings.save();
                self.park_blocked = false;
                self.notice(&format!("park {m}"), false);
            }
            ["continue", v @ ("on" | "off")] => {
                self.settings.park_continue = *v == "on";
                self.settings.save();
                self.notice(&format!("after a park the fresh session {}", if *v == "on" { "carries on" } else { "waits for you" }), false);
            }
            ["now"] | ["continue"] => {
                if self.turn_open || self.parking.is_some() {
                    return self.notice("a turn is running; park when it finishes", true);
                }
                self.continue_once = w[0] == "continue";
                self.park_blocked = false;
                self.begin_park(None);
            }
            _ => self.notice("usage: /park [now|continue] | auto|ask|off | continue on|off", true),
        }
    }

    pub fn status(&self) -> String {
        let trust = self.trust_profile();
        let ent = self.header.entropy.as_ref().map(|(s, b)| format!("{} {s:.2} · ", b.to_lowercase())).unwrap_or_default();
        format!(
            "model {} · {ent}crew {} · trust {}{} · mode {}{}",
            if self.header.model.is_empty() { "default" } else { &self.header.model }, if self.settings.crew { "on" } else { "off" },
            if trust.is_empty() { "?" } else { &trust }, if self.settings.trust.is_some() { " (set)" } else { " (from entropy)" }, self.settings.mode, self.caps_line
        )
    }

    // ---- paint -------------------------------------------------------------------------------
    pub fn busy_label(&self) -> &'static str {
        if matches!(self.overlay, Overlay::Permission(_)) || !self.asks.is_empty() {
            "awaiting approval"
        } else if self.parking.is_some() {
            "parking"
        } else if self.turn_open {
            "working"
        } else {
            ""
        }
    }

    pub fn needs_tick(&self) -> bool {
        matches!(&self.overlay, Overlay::Permission(p) if !p.armed())
    }

    pub fn draw(&mut self, f: &mut TermFrame) {
        self.dirty = false;
        let a = f.area();
        if a.width < 4 || a.height < 6 {
            return;
        }
        let comp_h = self.composer.height(a.width).min(a.height.saturating_sub(4));
        let tr_h = a.height.saturating_sub(3 + comp_h);
        self.header.session = self.session.clone().unwrap_or_default();
        let (ha, da) = (Rect { height: 2, ..a }, Rect { y: a.y + 2, height: 1, ..a });
        let ta = Rect { y: a.y + 3, height: tr_h, ..a };
        let ca = Rect { y: a.y + 3 + tr_h, height: comp_h, ..a };
        self.t.layout(&self.th, a.width, tr_h);
        let buf = f.buffer_mut();
        self.header.draw(ha, buf, &self.th);
        draw_divider(da, buf, &self.th, self.busy_label());
        ratatui::widgets::Widget::render(View(&self.t), ta, buf);
        let focused = matches!(self.overlay, Overlay::None);
        let cursor = self.composer.draw(ca, buf, &self.th, focused);
        if self.composer.completing() && focused {
            let n = self.composer.dropdown.len().min(8) as u16;
            let drop = Rect { y: ca.y.saturating_sub(n).max(ta.y), height: n.min(tr_h), ..a };
            self.composer.draw_dropdown(drop, buf, &self.th);
        }
        match &self.overlay {
            Overlay::Permission(p) => p.draw(Rect { y: a.y + 3, height: a.height - 3, ..a }, buf, &self.th),
            Overlay::Model(p) | Overlay::Sessions(p) => p.draw(a, buf, &self.th),
            Overlay::None => f.set_cursor_position(cursor),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::Glyphs;
    use crossterm::event::{KeyEvent, KeyModifiers, MouseEvent};
    use ratatui::{backend::TestBackend, Terminal};
    use std::sync::mpsc::Receiver;

    const CLIENT: &str = "rabble-tui-test";

    fn app_with(start: Start) -> (App, Receiver<String>) {
        let (h, rx) = Handle::detached();
        let cfg = Config { cwd: "/w".into(), start, client: CLIENT.into() };
        (App::new(cfg, Theme::new(None, true, Glyphs::SAFE), Settings::default(), h), rx)
    }
    fn live() -> (App, Receiver<String>) {
        let (mut a, rx) = app_with(Start::New { resume: None });
        a.session = Some("s1".into());
        (a, rx)
    }
    fn feed(a: &mut App, line: &str) {
        let f = Frame::parse(line).unwrap_or_else(|e| panic!("{e}: {line}"));
        a.on_net(Net::Frame(f));
    }
    fn ev(a: &mut App, line: &str) {
        let mut v: serde_json::Value = serde_json::from_str(line).unwrap();
        v["session"] = "s1".into();
        feed(a, &v.to_string());
    }
    fn key(c: KeyCode) -> Event {
        Event::Key(KeyEvent::new(c, KeyModifiers::NONE))
    }
    fn type_in(a: &mut App, s: &str) {
        for c in s.chars() {
            a.on_input(key(KeyCode::Char(c)));
        }
    }
    fn say(a: &mut App, s: &str) {
        type_in(a, s);
        a.on_input(key(KeyCode::Enter));
    }
    fn sent(rx: &Receiver<String>) -> Vec<serde_json::Value> {
        rx.try_iter().map(|l| serde_json::from_str(&l).unwrap()).collect()
    }
    fn screen(a: &mut App, w: u16, h: u16) -> String {
        let mut t = Terminal::new(TestBackend::new(w, h)).unwrap();
        t.draw(|f| a.draw(f)).unwrap();
        let b = t.backend().buffer();
        (0..h).map(|y| (0..w).map(|x| b[(x, y)].symbol().to_owned()).collect::<String>().trim_end().to_owned()).collect::<Vec<_>>().join("\n")
    }
    fn ask(id: &str) -> String {
        format!(r#"{{"type":"PermissionAsk","id":"{id}","organ":"rabble","tool":"Bash","input":{{"command":"ls"}}}}"#)
    }

    #[test]
    fn hello_creates_a_session_with_the_saved_settings_and_attaches_on_request() {
        let (mut a, rx) = app_with(Start::New { resume: None });
        a.settings.model = Some("haiku".into());
        feed(&mut a, r#"{"type":"hello","version":1,"engine":"e","user":"u","features":[]}"#);
        let s = sent(&rx);
        assert_eq!((s[0]["type"].as_str(), s[0]["model"].as_str(), s[0]["cwd"].as_str()), (Some("create_session"), Some("haiku"), Some("/w")));
        assert!(s[0]["id"].is_string(), "creation is a matched request");
        let (mut b, rx) = app_with(Start::Attach("s9".into()));
        feed(&mut b, r#"{"type":"hello","version":1,"engine":"e","user":"u"}"#);
        feed(&mut b, r#"{"type":"hello","version":1,"engine":"e","user":"u"}"#); // a second hello does not start twice
        assert_eq!(sent(&rx).iter().filter(|m| m["type"] == "attach").count(), 1);
        assert_eq!(b.session.as_deref(), Some("s9"));
    }

    #[test]
    fn text_is_rendered_once_and_the_header_shows_the_model() {
        let (mut a, _rx) = live();
        ev(&mut a, r#"{"type":"SessionStarted","session_id":"abc","model":"claude-haiku","cwd":"/w"}"#);
        ev(&mut a, r#"{"type":"TextDelta","organ":"rabble","text":"Hel"}"#);
        ev(&mut a, r#"{"type":"TextDelta","organ":"rabble","text":"lo there"}"#);
        ev(&mut a, r#"{"type":"TextBlock","organ":"rabble","text":"Hello there"}"#);
        let s = screen(&mut a, 80, 20);
        assert_eq!(s.matches("Hello there").count(), 1, "{s}");
        assert!(s.lines().next().unwrap().contains("claude-haiku"), "{s}");
    }

    #[test]
    fn a_tool_card_gets_its_result() {
        let (mut a, _rx) = live();
        ev(&mut a, r#"{"type":"ToolCall","organ":"rabble","id":"t1","name":"Bash","input":{"command":"ls"}}"#);
        ev(&mut a, r#"{"type":"ToolResult","organ":"rabble","id":"t1","ok":true,"summary":"3 files"}"#);
        let s = screen(&mut a, 80, 20);
        assert!(s.contains("Bash  ls -> 3 files"), "{s}");
    }

    #[test]
    fn organ_turns_become_notices_and_background_turns_are_named() {
        let (mut a, _rx) = live();
        ev(&mut a, r#"{"type":"OrganStarted","organ":"gobble","tool_use_id":"u","task_id":"k","brief":"map it"}"#);
        ev(&mut a, r#"{"type":"OrganDone","organ":"gobble","task_id":"k","status":"failed"}"#);
        ev(&mut a, r#"{"type":"TurnDone","stop_reason":"end_turn","origin":"background"}"#);
        let s = screen(&mut a, 80, 20);
        assert!(s.contains("gobble started: map it") && s.contains("gobble failed") && s.contains("background turn finished"), "{s}");
    }

    #[test]
    fn model_and_mode_commands_reach_the_engine_and_stick() {
        let (mut a, rx) = live();
        a.settings.path = None;
        say(&mut a, "/model opus");
        say(&mut a, "/mode plan");
        a.on_input(Event::Key(KeyEvent::new(KeyCode::BackTab, KeyModifiers::SHIFT)));
        say(&mut a, "/mode nope");
        let s = sent(&rx);
        assert_eq!((s[0]["type"].as_str(), s[0]["model"].as_str(), s[0]["session"].as_str()), (Some("set_model"), Some("opus"), Some("s1")));
        assert_eq!((s[1]["type"].as_str(), s[1]["mode"].as_str()), (Some("set_permission_mode"), Some("plan")));
        assert_eq!(s[2]["mode"], "auto", "Shift+Tab cycles to the next mode");
        assert_eq!(s.len(), 3);
        assert_eq!(a.settings.model.as_deref(), Some("opus"));
        let screen = screen(&mut a, 100, 20);
        assert!(screen.contains("usage: /mode default|acceptEdits|plan|auto") && screen.contains("model opus"), "{screen}");
    }

    #[test]
    fn model_picker_opens_without_an_argument_and_enter_chooses() {
        let (mut a, rx) = live();
        say(&mut a, "/model");
        assert!(matches!(a.overlay, Overlay::Model(_)));
        a.on_input(key(KeyCode::Down));
        a.on_input(key(KeyCode::Enter));
        assert!(matches!(a.overlay, Overlay::None));
        assert_eq!(sent(&rx)[0]["model"], "opus");
    }

    #[test]
    fn model_trust_crew_commands() {
        let (mut a, rx) = live();
        say(&mut a, "/trust strict");
        say(&mut a, "/trust auto");
        say(&mut a, "/trust bogus");
        say(&mut a, "/model default");
        let s = sent(&rx);
        assert_eq!((s[0]["type"].as_str(), s[0]["profile"].as_str()), (Some("set_trust"), Some("strict")));
        assert!(s[1]["profile"].is_null());
        assert_eq!(s[2]["type"], "set_model");
        assert!(s[2]["model"].is_null());
        assert_eq!(s.len(), 3);
        // crew restarts on the same Claude session
        ev(&mut a, r#"{"type":"SessionStarted","session_id":"sdk-1","model":"m","cwd":"/w"}"#);
        say(&mut a, "/crew on");
        let s = sent(&rx);
        assert_eq!(s[0]["type"], "close_session");
        assert_eq!((s[1]["type"].as_str(), s[1]["resume"].as_str(), s[1]["crew"].as_bool()), (Some("create_session"), Some("sdk-1"), Some(true)));
        assert!(a.t.items().any(|i| matches!(i, crate::widgets::transcript::Item::Notice { text, .. } if text.starts_with("crew on"))));
    }

    #[test]
    fn an_unknown_slash_command_goes_to_the_engine_as_typed() {
        let (mut a, rx) = live();
        say(&mut a, "/compact now");
        let s = sent(&rx);
        assert_eq!((s[0]["type"].as_str(), s[0]["text"].as_str()), (Some("send"), Some("/compact now")));
        assert!(a.turn_open);
    }

    #[test]
    fn ctrl_c_twice_quits_once_is_a_hint() {
        let (mut a, _rx) = live();
        let c = Event::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL));
        a.on_input(c.clone());
        assert!(!a.quit);
        assert!(screen(&mut a, 80, 20).contains("press Ctrl-C again to quit"));
        a.on_input(c);
        assert!(a.quit);
        let (mut b, _) = live();
        b.on_input(Event::Key(KeyEvent::new(KeyCode::Char('d'), KeyModifiers::CONTROL)));
        assert!(b.quit);
    }

    #[test]
    fn engine_trouble_becomes_notices_not_crashes() {
        let (mut a, rx) = app_with(Start::New { resume: None });
        say(&mut a, "hello"); // no session yet
        assert!(sent(&rx).is_empty() && !a.turn_open);
        ev(&mut a, r#"{"type":"Error","text":"boom"}"#);
        feed(&mut a, r#"{"type":"Error","text":"boom 2"}"#);
        a.on_net(Net::Malformed { why: "bad".into(), line: "{x".into() });
        a.on_net(Net::Closed("eof".into()));
        let s = screen(&mut a, 100, 30);
        assert!(s.contains("no engine session yet") && s.contains("boom 2") && s.contains("could not read") && s.contains("connection lost") && s.contains("offline"), "{s}");
    }

    #[test]
    fn copy_puts_the_last_reply_on_the_clipboard_effect() {
        let (mut a, _rx) = live();
        say(&mut a, "/copy");
        assert!(a.effects.is_empty());
        ev(&mut a, r#"{"type":"TextBlock","organ":"rabble","text":"the reply"}"#);
        say(&mut a, "/copy");
        assert_eq!(a.effects, [Effect::Copy("the reply".into())]);
    }

    #[test]
    fn slash_completion_uses_capabilities_and_help_lists_commands() {
        let (mut a, _rx) = live();
        ev(&mut a, r#"{"type":"Capabilities","slash_commands":["compact","init"],"skills":["a"],"mcp":[{"name":"x","status":"connected"}],"plugins":[],"problems":["mcp x: slow"]}"#);
        type_in(&mut a, "/com");
        assert_eq!(a.composer.dropdown, ["/compact"]);
        a.on_input(key(KeyCode::Tab));
        assert_eq!(a.composer.text(), "/compact ");
        a.composer.set_text("");
        say(&mut a, "/help");
        say(&mut a, "/status");
        let s = screen(&mut a, 140, 40);
        assert!(s.contains("/resume") && s.contains("go to Claude Code as typed") && s.contains("2 commands · 1 skills · mcp 1/1") && s.contains("mcp x: slow"), "{s}");
    }

    #[test]
    fn permission_ask_answer_and_dismissal_by_another_client() {
        let (mut a, rx) = live();
        ev(&mut a, &ask("a1"));
        ev(&mut a, &ask("a2"));
        assert!(matches!(&a.overlay, Overlay::Permission(p) if p.ask.id == "a1"));
        assert_eq!(a.busy_label(), "awaiting approval");
        // someone else answered the open one: our prompt closes and the next opens
        ev(&mut a, r#"{"type":"PermissionResolved","id":"a1","decision":"allow","by":"gui"}"#);
        assert!(matches!(&a.overlay, Overlay::Permission(p) if p.ask.id == "a2"));
        assert!(screen(&mut a, 100, 30).contains("permission allowed by gui"));
        // a replay after a gap must not reopen a1
        ev(&mut a, &ask("a1"));
        if let Overlay::Permission(p) = &mut a.overlay {
            p.armed_at = Instant::now() - std::time::Duration::from_secs(1);
        }
        a.on_input(key(KeyCode::Char('a')));
        let s = sent(&rx);
        assert_eq!(s.len(), 1);
        assert_eq!((s[0]["type"].as_str(), s[0]["ask_id"].as_str(), s[0]["decision"].as_str()), (Some("answer"), Some("a2"), Some("allow_session")));
        assert!(matches!(a.overlay, Overlay::None), "a1 was already resolved and stays closed");
    }

    #[test]
    fn typing_during_a_fresh_prompt_does_not_answer_it() {
        let (mut a, rx) = live();
        ev(&mut a, &ask("a1"));
        for c in "yyyy".chars() {
            a.on_input(key(KeyCode::Char(c)));
        }
        assert!(sent(&rx).is_empty() && matches!(a.overlay, Overlay::Permission(_)));
        a.on_input(key(KeyCode::Esc));
        assert_eq!(sent(&rx)[0]["decision"], "deny");
    }

    #[test]
    fn interrupt_clears_pending_asks_and_the_placeholder() {
        let (mut a, rx) = live();
        say(&mut a, "go");
        a.on_input(key(KeyCode::Esc));
        assert_eq!(sent(&rx).last().unwrap()["type"], "interrupt");
    }

    #[test]
    fn seq_tracks_events_only_and_other_sessions_are_ignored() {
        let (mut a, _rx) = live();
        feed(&mut a, r#"{"type":"attached","session":"s1","seq":99,"role":"owner","snapshot":{"busy":true,"model":"opus","mode":"plan"}}"#);
        assert_eq!(a.max_seq, 0, "attached.seq is the head, not a resume point");
        assert!(a.turn_open && a.header.model == "opus" && a.settings.mode == "plan");
        feed(&mut a, r#"{"type":"TextDelta","session":"s1","seq":4,"organ":"rabble","text":"x"}"#);
        feed(&mut a, r#"{"type":"TextDelta","session":"s1","seq":3,"organ":"rabble","text":"y"}"#);
        feed(&mut a, r#"{"type":"TextDelta","session":"other","seq":50,"organ":"rabble","text":"z"}"#);
        assert_eq!(a.max_seq, 4);
        assert!(a.t.last_reply().unwrap() == "xy");
    }

    #[test]
    fn own_messages_jump_to_the_bottom_others_do_not_yank_the_reader() {
        let (mut a, _rx) = live();
        ev(&mut a, &format!(r#"{{"type":"TextBlock","organ":"rabble","text":"{}"}}"#, "line\\n\\n".repeat(80)));
        screen(&mut a, 80, 20);
        a.t.scroll_by(-40);
        ev(&mut a, r#"{"type":"UserSaid","sender":"u","client":"gui","text":"from the phone"}"#);
        screen(&mut a, 80, 20);
        assert!(!a.t.follow);
        ev(&mut a, &format!(r#"{{"type":"UserSaid","sender":"u","client":"{CLIENT}","text":"mine"}}"#));
        screen(&mut a, 80, 20);
        assert!(a.t.follow);
    }

    #[test]
    fn resume_lists_live_sessions_from_the_matched_reply() {
        let (mut a, rx) = live();
        say(&mut a, "/resume");
        let req = sent(&rx).into_iter().find(|m| m["type"] == "list_sessions").unwrap();
        assert!(req["id"].is_string());
        let f = Frame::parse(r#"{"type":"sessions","id":"r1","sessions":[{"session":"s2","role":"participant","clients":2,"cwd":"/w","busy":true}]}"#).unwrap();
        a.on_net(Net::Reply { label: "resume-list".into(), frame: f });
        let Overlay::Sessions(p) = &mut a.overlay else { panic!("picker") };
        assert!(p.items[0].0.contains("s2") && p.items[0].1 == "attach:s2");
        a.on_input(key(KeyCode::Enter));
        let s = sent(&rx);
        assert!(s.iter().any(|m| m["type"] == "attach" && m["session"] == "s2"));
        assert_eq!(a.session.as_deref(), Some("s2"));
    }

    #[test]
    fn clear_closes_the_old_session_and_starts_a_fresh_one() {
        let (mut a, rx) = live();
        ev(&mut a, r#"{"type":"TextBlock","organ":"rabble","text":"old"}"#);
        say(&mut a, "/clear");
        let s = sent(&rx);
        assert_eq!((s[0]["type"].as_str(), s[0]["session"].as_str(), s[1]["type"].as_str()), (Some("close_session"), Some("s1"), Some("create_session")));
        assert!(a.session.is_none() && a.t.last_reply().is_none());
        feed(&mut a, r#"{"type":"session_created","session":"s2"}"#);
        ev(&mut a, r#"{"type":"TextBlock","organ":"rabble","text":"stale"}"#); // old session: ignored
        assert_eq!((a.session.as_deref(), a.t.last_reply()), (Some("s2"), None));
    }

    #[test]
    fn park_settings_and_status() {
        let (mut a, _rx) = live();
        say(&mut a, "/park ask");
        say(&mut a, "/park continue on");
        assert_eq!((a.settings.park.as_str(), a.settings.park_continue), ("ask", true));
        say(&mut a, "/park status");
        say(&mut a, "/park wat");
        let s = screen(&mut a, 140, 30);
        assert!(s.contains("park ask at 80%") && s.contains("usage: /park"), "{s}");
    }

    #[test]
    fn park_now_sends_the_park_prompt_then_restarts_from_the_handoff() {
        let dir = std::env::temp_dir().join(format!("rt-parkflow-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let (mut a, rx) = live();
        a.cfg.cwd = dir.to_string_lossy().into_owned();
        say(&mut a, "/park now");
        let sent1 = sent(&rx);
        let prompt = sent1.iter().find(|m| m["type"] == "send").unwrap()["text"].as_str().unwrap().to_owned();
        let path = prompt.split("exactly this path: ").nth(1).unwrap().lines().next().unwrap().to_owned();
        std::fs::write(&path, "x".repeat(400)).unwrap();
        ev(&mut a, r#"{"type":"TurnDone","stop_reason":"end_turn","origin":"observer"}"#);
        let s = sent(&rx);
        assert_eq!((s[0]["type"].as_str(), s[1]["type"].as_str()), (Some("close_session"), Some("create_session")));
        feed(&mut a, r#"{"type":"session_created","session":"s2"}"#);
        ev_to(&mut a, "s2", r#"{"type":"SessionStarted","session_id":"n","model":"m","cwd":"/w"}"#);
        let seed = sent(&rx).into_iter().find(|m| m["type"] == "send").unwrap();
        assert!(seed["text"].as_str().unwrap().contains("fresh session parked"));
        std::fs::remove_dir_all(&dir).ok();
    }

    fn ev_to(a: &mut App, s: &str, line: &str) {
        let mut v: serde_json::Value = serde_json::from_str(line).unwrap();
        v["session"] = s.into();
        feed(a, &v.to_string());
    }

    #[test]
    fn resize_between_frames_never_panics_and_rewraps() {
        let (mut a, _rx) = live();
        ev(&mut a, &format!(r#"{{"type":"TextBlock","organ":"rabble","text":"{}"}}"#, "word ".repeat(300)));
        for (w, h) in [(120, 40), (30, 10), (200, 70), (8, 7), (4, 6), (3, 3), (80, 24)] {
            let s = screen(&mut a, w, h);
            assert!(s.lines().count() <= h as usize);
        }
        assert!(a.t.follow, "still pinned after all that");
    }

    #[test]
    fn mouse_wheel_scrolls_one_to_one_and_focus_events_repin() {
        let (mut a, _rx) = live();
        ev(&mut a, &format!(r#"{{"type":"TextBlock","organ":"rabble","text":"{}"}}"#, "line\\n\\n".repeat(100)));
        screen(&mut a, 80, 20);
        let bottom = a.t.offset;
        let wheel = |k| Event::Mouse(MouseEvent { kind: k, column: 1, row: 1, modifiers: KeyModifiers::NONE });
        a.on_input(wheel(MouseEventKind::ScrollUp));
        assert_eq!(a.t.offset, bottom - 3);
        a.on_input(Event::FocusLost);
        a.on_input(Event::FocusGained);
        a.on_input(wheel(MouseEventKind::ScrollDown));
        assert!(a.t.offset >= bottom - 3);
    }

    #[test]
    fn a_recorded_turn_replays_into_a_readable_screen() {
        let (mut a, _rx) = live();
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/orchestra/tool-turn.jsonl");
        for l in std::fs::read_to_string(path).unwrap().lines().filter(|l| !l.trim().is_empty()) {
            ev(&mut a, l);
        }
        let s = screen(&mut a, 120, 30);
        assert!(s.contains("Bash") && !a.turn_open, "{s}");
    }

    #[test]
    fn nothing_the_shell_draws_needs_a_hex_literal_and_no_color_has_no_colour_cells() {
        let (mut a, _rx) = live();
        ev(&mut a, r#"{"type":"TextBlock","organ":"rabble","text":"**hi** `x`"}"#);
        let mut t = Terminal::new(TestBackend::new(60, 12)).unwrap();
        t.draw(|f| a.draw(f)).unwrap();
        let b = t.backend().buffer();
        assert!(b.content().iter().all(|c| c.fg == ratatui::style::Color::Reset && c.bg == ratatui::style::Color::Reset));
    }
}
