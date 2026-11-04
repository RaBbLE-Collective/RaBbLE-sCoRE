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
    sync::{mpsc::Sender, Arc},
    time::{Duration, Instant, SystemTime},
};

use crossterm::event::{Event, KeyCode, KeyEventKind, KeyModifiers, MouseEventKind};
use rabble_proto::{Decision, Down, Frame, LimitStatus, OrganStatus, SessionInfo, Snapshot, TurnOrigin, Up};
use rabble_face::{provider_of, FacePalette, Mood};
use ratatui::{layout::Rect, Frame as TermFrame};

use crate::{
    client::{Handle, Net},
    park,
    prefs::{self, Settings},
    sessions,
    theme::Theme,
    widgets::{
        composer::{Action, Composer},
        face::{safe_glyphs, FaceHub},
        header::{draw_divider, HeaderState},
        modal::{Ask, Permission, Picked, Picker, Reply},
        crew::{CrewView, ViewAction, PANEL_MIN_AREA, PANEL_WIDTH},
        transcript::{Transcript, View},
        FACE_COMPACT_MIN_AREA, FACE_COMPACT_ROWS, FACE_COMPACT_SLOT_WIDTH, FACE_MIN_AREA, FACE_ROWS, FACE_SLOT_WIDTH,
    },
};

pub const QUIT_WINDOW_MS: u128 = 2000;
/// How long an error or a denial holds the face's mood (Python `FLASH`).
pub const FLASH: Duration = Duration::from_millis(2500);
pub const HELP: [(&str, &str); 14] = [
    ("/model", "pick the model (or /model opus|sonnet|haiku|fable|default|<id>)"),
    ("/crew", "organs: /crew on|off (off = solo, cheapest) · /crew view = panel, then transcripts (Ctrl-G cycles)"),
    ("/trust", "auto-approve level: /trust strict|standard|trusted|auto (auto = earned from session entropy)"),
    ("/mode", "permission mode: /mode default|acceptEdits|plan|auto (Shift+Tab cycles)"),
    ("/status", "model, crew, trust and mode at a glance"),
    ("/park", "hand off to a fresh session: /park [now|continue] | auto|ask|off | continue on|off (parks itself at 80% context)"),
    ("/resume", "pick an earlier session"),
    ("/clear", "start a fresh session"),
    ("/copy", "copy the last reply (drag to select, Ctrl-C copies the selection, Ctrl-V pastes)"),
    ("/face", "show the full face"),
    ("/reload", "restart this shell on the same session (picks up a rebuilt binary; the engine is untouched)"),
    ("/help", "this list"),
    ("/quit", "leave (Ctrl-C twice also works)"),
    ("Ctrl-O", "expand or collapse tool cards"),
];

/// Makes a fresh engine connection (used to reconnect); `None` in tests and benches.
pub type Connector = Arc<dyn Fn() -> std::io::Result<Handle> + Send + Sync>;

/// Everything the run loop hands the app besides engine frames (those come on their own bounded
/// queue and are fed to `on_net`). Workers (file scan, session list, clipboard, reconnect) post
/// their results back as messages, so nothing slow ever runs on the render thread.
pub enum Msg {
    Input(Event, Instant),
    /// Engine frames are waiting on the net queue.
    Wake,
    /// Leave (signal, or the input thread died).
    Quit(String),
    Sessions(Vec<sessions::Row>),
    Files(Vec<String>),
    Clipboard(String),
    Reconnected(Handle),
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
    Crew(CrewView),
}

const FLOW_EVERY: Duration = Duration::from_millis(125); // 8 fps, like the Python divider
const USAGE_EVERY: Duration = Duration::from_secs(30);

pub struct App {
    pub th: Theme,
    pub t: Transcript,
    pub composer: Composer,
    pub header: HeaderState,
    pub settings: Settings,
    pub overlay: Overlay,
    pub crew: crate::crew::Crew,
    /// The crew side panel, when the pane is wide enough for it.
    pub crew_panel: bool,
    pub quit: bool,
    /// Set with `quit` by `/reload`: leave the terminal, then `exec` the (rebuilt) binary on this session.
    pub reload: bool,
    pub dirty: bool,
    flow_t0: Instant,
    flow_next: Instant,
    usage_at: Option<Instant>,
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
    /// Where the transcript was last painted; mouse selection maps cells through it.
    tr_rect: Rect,
    last_width: u16,
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
    /// Where workers post their results (set by the run loop).
    pub post: Option<Sender<Msg>>,
    pub connector: Option<Connector>,
    pub quit_reason: Option<String>,
    create_id: Option<String>,
    reconnecting: bool,
    reconnect_running: bool,
    /// The face (`None` without a palette that has the face's colours).
    pub face: Option<FaceHub>,
    streaming: bool,
    busy: HashSet<String>,
    flash: Option<(Mood, Instant)>,
    local: bool,
    gz: gaze::Steer,
}

/// Where RaBbLE's eyes go. The pointer wins while it moves; when it idles the entity picks what is most
/// interesting (`choose`), and with nothing to look at the face's own seeded saccades take over.
/// TUNING: every knob Mark may want is a `pub const` here.
mod gaze {
    use std::time::{Duration, Instant};

    /// Cadence of gaze updates (they ride the face tick; a gaze never repaints by itself).
    pub const EVERY: Duration = Duration::from_millis(100);
    /// The pointer counts as active this long after its last motion; the target then lapses over `POINTER_TTL`.
    pub const POINTER_ACTIVE: Duration = Duration::from_millis(2000);
    pub const POINTER_TTL: Duration = Duration::from_millis(1800);
    /// How often the entity re-chooses what to look at (low, so it never jitters), and how long it must stay
    /// on one thing before it may switch (hysteresis).
    pub const ATTN_EVERY: Duration = Duration::from_millis(500);
    pub const ATTN_HOLD: Duration = Duration::from_millis(1500);
    pub const ATTN_TTL: Duration = Duration::from_millis(1000);
    /// How long after the last keystroke the composer stays interesting, and after a card changed the card does.
    pub const TYPING_HOLD: Duration = Duration::from_millis(2000);
    pub const CARD_HOLD: Duration = Duration::from_millis(2500);
    /// Cells of offset from a face's centre that turn the eyes fully (x columns, y rows).
    pub const RANGE_COLS: f64 = 40.0;
    pub const RANGE_ROWS: f64 = 12.0;

    pub type Cell = (f64, f64);

    /// What it is looking at, most interesting first.
    #[derive(Clone, Copy, PartialEq, Debug)]
    pub enum Focus {
        Modal,
        Typing,
        Card,
        Text,
    }

    #[derive(Default)]
    pub struct Scene {
        pub modal: Option<Cell>,
        pub typing: bool,
        pub cursor: Cell,
        pub card: bool,
        pub streaming: bool,
        pub newest: Option<Cell>,
    }

    /// App state -> the screen cell worth looking at, if any.
    pub fn choose(s: &Scene) -> Option<(Focus, Cell)> {
        if let Some(c) = s.modal {
            return Some((Focus::Modal, c));
        }
        if s.typing {
            return Some((Focus::Typing, s.cursor));
        }
        match (s.card, s.streaming, s.newest) {
            (true, _, Some(c)) => Some((Focus::Card, c)),
            (_, true, Some(c)) => Some((Focus::Text, c)),
            _ => None,
        }
    }

    /// A screen cell seen from a face centre -> the face's normalized target.
    pub fn aim(target: Cell, centre: Cell) -> Cell {
        (((target.0 - centre.0) / RANGE_COLS).clamp(-1.0, 1.0), ((target.1 - centre.1) / RANGE_ROWS).clamp(-1.0, 1.0))
    }

    #[derive(Default)]
    pub struct Steer {
        pub pointer: Option<((u16, u16), Instant)>,
        pub typed_at: Option<Instant>,
        pub card_at: Option<Instant>,
        pub cursor: (u16, u16),
        pub modal: Option<Cell>,
        /// Face centres on screen: mini, full.
        pub centres: [Option<Cell>; 2],
        /// The newest transcript line (row in the view, cells of text) as of the last layout; the face tick runs
        /// before layout, when streaming text has the transcript stale, so it reads this instead.
        pub newest: Option<(u16, usize)>,
        pub next: Option<Instant>,
        pub attn_next: Option<Instant>,
        cur: Option<(Focus, Cell)>,
        since: Option<Instant>,
        /// What was last sent to each face (None = handed back to the saccades).
        pub aimed: [Option<Cell>; 2],
    }

    impl Steer {
        pub fn current(&self) -> Option<Cell> {
            self.cur.map(|(_, c)| c)
        }

        /// Hysteresis: the same focus follows its target; a different one (or none) waits out `ATTN_HOLD`.
        pub fn pick(&mut self, want: Option<(Focus, Cell)>, now: Instant) -> Option<Cell> {
            match (self.cur, want) {
                (Some((a, _)), Some((b, c))) if a == b => self.cur = Some((b, c)),
                (cur, want) => {
                    if cur.is_none() || self.since.is_none_or(|t| now.saturating_duration_since(t) >= ATTN_HOLD) {
                        self.cur = want;
                        self.since = Some(now);
                    }
                }
            }
            self.cur.map(|(_, c)| c)
        }
    }
}

/// The palette names the face reads; a palette without them gets no face.
fn palette_has_face_colours(p: &rabble_theme::Palette) -> bool {
    ["bg", "text", "cyan", "magenta", "violet", "pink", "muted"].iter().all(|n| p.get(n).is_some())
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
            th, t: Transcript::new(), composer, header, settings, overlay: Overlay::None, crew: crate::crew::Crew::new(), crew_panel: false, quit: false, reload: false, dirty: true, flow_t0: Instant::now(), flow_next: Instant::now(), usage_at: None, effects: vec![], malformed: 0,
            session: None, max_seq: 0, turn_open: false, engine_ok: true, cfg, out, asks: VecDeque::new(), seen_asks: HashSet::new(), last_ctrl_c: None, tr_rect: Rect::default(), last_width: 0,
            sdk_session: None, started: false, live: vec![], parking: None, park_pending: false, park_warned: false, park_blocked: false, seed: None,
            continue_once: false, entropy_trust: None, caps_line: String::new(), focused: true,
            post: None, connector: None, quit_reason: None, create_id: None, reconnecting: false, reconnect_running: false,
            face: None, streaming: false, busy: HashSet::new(), flash: None, local: false, gz: gaze::Steer::default(),
        }.with_face()
    }

    fn with_face(mut self) -> App {
        self.face = self.th.pal.as_ref().filter(|p| palette_has_face_colours(p)).map(|p| FaceHub::new(FacePalette::from_palette(p), safe_glyphs(), Instant::now()));
        self.sync_face();
        self
    }

    // ---- the face -----------------------------------------------------------------------------
    fn flash(&mut self, m: Mood) {
        self.flash = Some((m, Instant::now() + FLASH));
    }

    /// Python `_mood()`: a flash, else streaming, else something running, else typing, else idle.
    fn derive_mood(&self, now: Instant) -> Mood {
        if let Some((m, until)) = self.flash {
            if now < until {
                return m;
            }
        }
        if self.streaming {
            Mood::Streaming
        } else if !self.busy.is_empty() {
            Mood::Working
        } else if !self.composer.is_empty() {
            Mood::Typing
        } else {
            Mood::Idle
        }
    }

    /// The model the provider tint follows (Python `_model_for_entropy`).
    fn model_for_face(&self) -> Option<&str> {
        let m = self.header.model.as_str();
        if m.is_empty() || m == "default" { self.settings.model.as_deref() } else { Some(m) }
    }

    /// Push mood and provider into the face. Cheap: both are no-ops when unchanged.
    fn sync_face(&mut self) {
        let (now, provider) = (Instant::now(), provider_of(self.model_for_face(), self.local));
        let mood = self.derive_mood(now);
        if let Some(f) = &mut self.face {
            f.set_provider(provider);
            f.set_mood(mood, now);
        }
    }

    /// When the run loop should next call `tick_face` (faces that are on screen only).
    pub fn face_due(&self) -> Option<Instant> {
        // without colour the face is a static word: no frames to compute, nothing to wake for
        self.face.as_ref().filter(|_| self.th.color).and_then(|f| f.due())
    }

    /// When the flowing divider and border next need a frame (and the usage cache a re-read).
    pub fn flow_due(&self) -> Option<Instant> {
        let usage = self.usage_at.map_or_else(Instant::now, |t| t + USAGE_EVERY);
        Some(if self.th.animates() { self.flow_next.min(usage) } else { usage })
    }

    pub fn tick_flow(&mut self, now: Instant) {
        if self.usage_at.is_none_or(|t| now.saturating_duration_since(t) >= USAGE_EVERY) {
            self.usage_at = Some(now);
            self.header.usage = Some(crate::usage::read());
            self.dirty = true; // the reset countdowns move with the clock, with or without colour
        }
        if self.th.animates() && now >= self.flow_next {
            self.flow_next = now + FLOW_EVERY;
            self.dirty = true;
        }
    }

    /// Compute due face frames; a paint is requested only if a cell changed. The run loop calls this on
    /// iterations that carried no input, so it never sits in front of a key or a wheel notch.
    #[cfg(test)]
    fn hub_full_cells_empty(&self) -> bool {
        self.face.as_ref().and_then(|h| h.full.as_ref()).is_none_or(|f| f.cells.is_empty())
    }

    pub fn tick_face(&mut self, now: Instant) {
        if self.face.is_none() || !self.th.color {
            return;
        }
        self.sync_face();
        self.steer_gaze(now);
        if self.face.as_mut().is_some_and(|f| f.tick(now)) {
            self.dirty = true;
        }
    }

    /// Point the faces at the pointer while it moves, else at what the entity finds interesting, else hand the
    /// gaze back to the saccades. Runs on the face tick at `gaze::EVERY`; it never sets `dirty` itself, so a
    /// gaze only shows on the next face frame. Re-applying each time also reaches a `/face` opened later.
    fn steer_gaze(&mut self, now: Instant) {
        if self.gz.next.is_some_and(|t| now < t) {
            return;
        }
        self.gz.next = Some(now + gaze::EVERY);
        let pointer = self.gz.pointer.filter(|(_, at)| now.saturating_duration_since(*at) < gaze::POINTER_ACTIVE);
        let (target, ttl) = if let Some(((x, y), _)) = pointer {
            (Some((x as f64 + 0.5, y as f64 + 0.5)), gaze::POINTER_TTL)
        } else {
            if self.gz.attn_next.is_none_or(|t| now >= t) {
                self.gz.attn_next = Some(now + gaze::ATTN_EVERY);
                let want = gaze::choose(&self.gaze_scene(now));
                self.gz.pick(want, now);
            }
            (self.gz.current(), gaze::ATTN_TTL)
        };
        let centres = self.gz.centres;
        let Some(hub) = &mut self.face else { return };
        let aim = |i: usize| target.zip(centres[i]).map(|(t, c)| gaze::aim(t, c));
        let aimed = [aim(0), aim(1)];
        hub.mini.set_gaze(aimed[0], Some(ttl), now);
        if let Some(f) = &mut hub.full {
            f.set_gaze(aimed[1], Some(ttl), now);
        }
        self.gz.aimed = aimed;
    }

    fn gaze_scene(&self, now: Instant) -> gaze::Scene {
        let within = |t: Option<Instant>, d: Duration| t.is_some_and(|t| now.saturating_duration_since(t) < d);
        let r = self.tr_rect;
        gaze::Scene {
            modal: self.gz.modal,
            typing: within(self.gz.typed_at, gaze::TYPING_HOLD),
            cursor: (self.gz.cursor.0 as f64 + 0.5, self.gz.cursor.1 as f64 + 0.5),
            card: within(self.gz.card_at, gaze::CARD_HOLD),
            streaming: self.streaming,
            newest: self.gz.newest.map(|(row, w)| ((r.x as usize + w) as f64, (r.y + row) as f64 + 0.5)),
        }
    }

    /// A passing note that must not split a reply that is still streaming.
    fn brief(&mut self, text: &str, error: bool) {
        self.t.notice_quiet(text, error);
        self.dirty = true;
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
        self.create_id = Some(self.out.request(
            &Up::CreateSession {
                cwd: Some(self.cfg.cwd.clone()), resume, continue_last: None, model: self.settings.model.clone(),
                mode: Some(self.settings.mode.clone()), crew: Some(crew), trust: self.settings.trust.clone(), provider: None,
            },
            "create",
        ));
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
        self.on_net_inner(n);
        self.sync_face();
    }

    fn on_net_inner(&mut self, n: Net) {
        match n {
            Net::Frame(f) => self.on_frame(f),
            Net::Reply { label, frame } => match (label.as_str(), &frame.msg) {
                ("resume-list", Down::Sessions { sessions }) => {
                    self.live = sessions.clone();
                    self.spawn_sessions();
                }
                // a creation we have since replaced (two quick /clear): close what it made, adopt nothing
                ("create", Down::SessionCreated {}) if frame.id != self.create_id => {
                    if let Some(s) = &frame.session {
                        self.out.send(&Up::CloseSession { session: s.clone() });
                    }
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
                let again = if self.connector.is_some() { "reconnecting" } else { "restart rabble to reconnect" };
                self.notice(&format!("engine connection lost: {why}; {again} (the session keeps running in the engine)"), true);
                self.start_reconnect();
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
                } else if std::mem::take(&mut self.reconnecting) {
                    // back after a drop: ask only for what we have not seen (max seq of events)
                    match self.session.clone() {
                        Some(session) => {
                            self.out.request(&Up::Attach { session, since_seq: Some(self.max_seq) }, "attach");
                        }
                        None => self.create(None, self.settings.crew),
                    }
                }
                return;
            }
            (Down::Sessions { sessions }, _) => {
                self.live = sessions.clone();
                return;
            }
            (Down::Error { .. }, Some(s)) if self.session.as_deref() != Some(s.as_str()) => return, // another session's trouble
            (Down::Error { text }, _) => {
                self.streaming = false;
                self.flash(Mood::Error);
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
                self.crew.clear(); // the replay that follows refills it
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
            Down::TextDelta { organ, text } => {
                self.crew.delta(&organ, &text);
                self.streaming = true;
                self.t.delta(&organ, &text)
            }
            Down::TextBlock { organ, text } => {
                self.crew.block(&organ, &text);
                self.streaming = false;
                self.t.block(&organ, &text)
            }
            Down::Thinking { .. } => self.t.thinking(),
            Down::ToolCall { organ, id, name, input } => {
                self.crew.tool_call(&organ, &id, &name, &input);
                self.streaming = false;
                self.gz.card_at = Some(Instant::now());
                self.busy.insert(id.clone());
                self.t.tool_call(&organ, &id, &name, &input)
            }
            Down::ToolResult { id, ok, summary, .. } => {
                self.crew.tool_result(&id, ok, &summary);
                self.busy.remove(&id);
                self.gz.card_at = Some(Instant::now());
                self.t.tool_result(&id, ok, &summary)
            }
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
                    self.crew.ask(&organ, &id, &tool);
                    self.asks.push_back(Ask { id, organ, tool, input, title, description, blocked_path });
                    self.next_ask();
                }
            }
            Down::PermissionResolved { id, decision, by } => {
                self.crew.resolved(&id, &format!("{decision:?}").to_lowercase());
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
            Down::OrganStarted { organ, brief, task_id, .. } => {
                self.crew.started(&organ, task_id, &brief);
                self.busy.insert(format!("organ:{organ}"));
                self.notice(&format!("{organ} started: {brief}"), false)
            }
            Down::OrganDone { organ, status, result, .. } => {
                self.crew.done(&organ, &status, &result);
                self.busy.remove(&format!("organ:{organ}"));
                let (word, err) = match status {
                    OrganStatus::Completed => ("completed", false),
                    OrganStatus::Failed => ("failed", true),
                    OrganStatus::Killed => ("killed", true),
                    OrganStatus::Unknown => ("ended", true),
                };
                self.notice(&format!("{organ} {word}"), err);
            }
            Down::OrganProgress { organ, usage, last_tool_name, .. } => self.crew.progress(&organ, usage.as_ref(), last_tool_name.as_deref()),
            Down::Narration { organ, text, .. } => self.crew.note(&organ, &text),
            Down::ModelUsed { model } => self.header.model = model,
            Down::EntropyChanged { score, band, trust, reason, base, stability, local, capability, categories, .. } => {
                self.local = local;
                self.header.local = local;
                self.header.capability = Some(capability);
                self.header.categories = categories.into_iter().collect();
                self.header.stability = Some(stability);
                if let Some(f) = &mut self.face {
                    f.set_signal(base as f64, stability as f64);
                }
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
                self.streaming = false;
                self.t.end_turn();
                if origin == TurnOrigin::Observer {
                    self.busy.clear();
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

    /// Ctrl-G: hidden -> side panel -> transcripts -> hidden.
    fn cycle_crew_view(&mut self) {
        if matches!(self.overlay, Overlay::Crew(_)) {
            self.overlay = Overlay::None;
            self.crew_panel = false;
        } else if !matches!(self.overlay, Overlay::None) {
        } else if self.crew_panel || self.cfg_narrow() {
            self.crew_panel = false;
            self.overlay = Overlay::Crew(CrewView::new(self.crew.members.iter().position(|m| m.state.live() && m.name != crate::crew::LEAD).unwrap_or(0)));
        } else {
            self.crew_panel = true;
        }
        self.dirty = true;
    }

    /// Too narrow for the panel: skip straight to the viewer.
    fn cfg_narrow(&self) -> bool {
        self.last_width < PANEL_MIN_AREA
    }

    // ---- permissions -------------------------------------------------------------------------
    fn next_ask(&mut self) {
        if matches!(self.overlay, Overlay::Crew(_)) && !self.asks.is_empty() {
            self.overlay = Overlay::None; // an approval outranks reading
        }
        if !matches!(self.overlay, Overlay::None) {
            return;
        }
        if let Some(a) = self.asks.pop_front() {
            self.overlay = Overlay::Permission(Box::new(Permission::new(a)));
            self.gz.card_at = Some(Instant::now());
        }
    }

    fn answer(&mut self, r: Reply) {
        let Overlay::Permission(p) = std::mem::replace(&mut self.overlay, Overlay::None) else { return };
        let (id, decision) = (p.ask.id.clone(), r.decision.clone());
        self.to_session("answer", |s| Up::Answer { session: s, ask_id: id, decision: r.decision, reason: r.reason, answers: r.answers });
        if decision == Decision::Deny {
            self.flash(Mood::Denied);
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
        self.crew.clear();
        self.turn_open = false;
        // the old session's in-flight tools and stream never finish in the new one (Python clears both here)
        self.streaming = false;
        self.busy.clear();
        self.max_seq = 0;
        self.park_pending = false;
        self.park_warned = false;
        if clear {
            self.t.clear();
            self.header.context_pct = None;
            self.header.entropy = None;
            self.header.capability = None;
            self.header.categories.clear();
            self.header.stability = None;
            self.entropy_trust = None;
            self.sync_header();
            if let Some(f) = &mut self.face {
                f.clear_signal();
            }
        }
        self.create(resume, self.settings.crew);
    }

    // ---- input -------------------------------------------------------------------------------
    pub fn on_msg(&mut self, m: Msg) {
        match m {
            Msg::Input(e, _) => self.on_input(e),
            Msg::Wake => {}
            Msg::Quit(why) => {
                self.quit_reason = Some(why);
                self.quit = true;
            }
            Msg::Sessions(rows) => {
                self.dirty = true;
                if matches!(self.overlay, Overlay::None) {
                    self.open_session_picker(rows);
                } else {
                    // never replace a prompt that is waiting for an answer
                    self.notice("session list is ready: run /resume again when the open prompt is closed", false);
                }
            }
            Msg::Files(v) => {
                self.dirty = true;
                self.composer.set_files(v);
            }
            Msg::Clipboard(t) => {
                self.dirty = true;
                if !t.is_empty() && matches!(self.overlay, Overlay::None) {
                    self.composer.insert_str(&t);
                    self.pump_composer();
                }
            }
            Msg::Reconnected(h) => {
                self.dirty = true;
                self.out = h;
                self.engine_ok = true;
                self.header.connected = true;
                self.reconnecting = true;
                self.reconnect_running = false;
                self.notice("reconnected", false);
            }
        }
    }

    fn spawn_sessions(&mut self) {
        let Some(post) = self.post.clone() else { return self.notice("/resume: no worker channel", true) };
        let (cwd, home) = (self.cfg.cwd.clone(), PathBuf::from(std::env::var_os("HOME").unwrap_or_default()));
        std::thread::spawn(move || {
            let _ = post.send(Msg::Sessions(sessions::recent(&home, std::path::Path::new(&cwd), 20)));
        });
    }

    /// Start the file scan an `@` completion is waiting for, off the render thread.
    fn pump_composer(&mut self) {
        if let Some((cwd, cap)) = self.composer.take_scan_request() {
            match self.post.clone() {
                Some(post) => {
                    std::thread::spawn(move || {
                        let _ = post.send(Msg::Files(crate::widgets::composer::scan_files(std::path::Path::new(&cwd), cap)));
                    });
                }
                None => self.composer.set_files(vec![]),
            }
        }
    }

    fn start_reconnect(&mut self) {
        let (Some(conn), Some(post)) = (self.connector.clone(), self.post.clone()) else { return };
        if std::mem::replace(&mut self.reconnect_running, true) {
            return;
        }
        std::thread::spawn(move || {
            let mut delay = Duration::from_millis(250);
            loop {
                match conn() {
                    Ok(h) => {
                        let _ = post.send(Msg::Reconnected(h));
                        return;
                    }
                    Err(_) => {
                        std::thread::sleep(delay);
                        delay = (delay * 2).min(Duration::from_secs(5));
                    }
                }
            }
        });
    }

    /// Close the overlay and let the next queued permission ask take the screen.
    fn close_overlay(&mut self) {
        self.overlay = Overlay::None;
        self.next_ask();
    }

    pub fn on_input(&mut self, e: Event) {
        // pointer motion, clicks and drags change nothing here: no repaint, and (in the loop) no stalled face frame
        if let Event::Mouse(m) = &e {
            if !matches!(m.kind, MouseEventKind::ScrollDown | MouseEventKind::ScrollUp) {
                // remembered for the next face tick only: no repaint, not typing
                self.gz.pointer = Some(((m.column, m.row), Instant::now()));
                if matches!(self.overlay, Overlay::None) && self.select_mouse(*m) {
                    self.dirty = true;
                }
                return;
            }
        }
        self.dirty = true;
        match e {
            Event::Resize(..) => crate::widgets::face::reset_cell_aspect(),
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
                Overlay::None => {
                    self.composer.insert_str(&s);
                    self.pump_composer();
                }
                _ => {}
            },
            Event::Mouse(m) => {
                let down = match m.kind {
                    MouseEventKind::ScrollDown => Some(true),
                    MouseEventKind::ScrollUp => Some(false),
                    _ => None,
                };
                if let Some(down) = down {
                    match &mut self.overlay {
                        Overlay::None => self.t.wheel(down),
                        Overlay::Permission(p) => p.wheel(down),
                        Overlay::Crew(v) => v.wheel(down),
                        _ => {}
                    }
                }
            }
            // a held key must never answer a permission prompt, so repeats are ignored there
            Event::Key(k) if k.kind == KeyEventKind::Press || (k.kind == KeyEventKind::Repeat && !matches!(self.overlay, Overlay::Permission(_))) => self.on_key(k),
            _ => {}
        }
        self.pump_composer();
        self.sync_face();
    }

    /// Left-button selection in the transcript; true when something on screen changed.
    fn select_mouse(&mut self, m: crossterm::event::MouseEvent) -> bool {
        use crossterm::event::{MouseButton, MouseEventKind::{Down, Drag, Up}};
        let r = self.tr_rect;
        let inside = m.row >= r.y && m.row < r.y + r.height && m.column >= r.x && m.column < r.x + r.width;
        match m.kind {
            Down(MouseButton::Left) => {
                let had = self.t.sel.has();
                self.t.sel.clear();
                if inside {
                    if let Some(p) = self.t.pos_at(m.row - r.y, m.column - r.x) {
                        self.t.select_press(p);
                    }
                }
                had || self.t.sel.has()
            }
            Drag(MouseButton::Left) => {
                if !self.t.sel.dragging() {
                    return false; // the press was not in the transcript: nothing to extend or scroll
                }
                let (before, off) = (self.t.sel.ordered(), self.t.offset);
                // dragging past an edge scrolls the view one line per event
                let row = if m.row < r.y {
                    self.t.scroll_by(-1);
                    0
                } else if m.row >= r.y + r.height {
                    self.t.scroll_by(1);
                    r.height.saturating_sub(1)
                } else {
                    m.row - r.y
                };
                let col = m.column.saturating_sub(r.x).min(r.width.saturating_sub(1));
                if let Some(p) = self.t.pos_at(row, col) {
                    self.t.sel.drag(p);
                }
                self.t.sel.ordered() != before || self.t.offset != off
            }
            Up(MouseButton::Left) => {
                self.t.sel.release();
                false
            }
            _ => false,
        }
    }

    fn on_key(&mut self, k: crossterm::event::KeyEvent) {
        if matches!(self.overlay, Overlay::None) {
            self.gz.typed_at = Some(Instant::now());
        }
        match &mut self.overlay {
            Overlay::Permission(p) => {
                if let Some(r) = p.key(k) {
                    self.answer(r);
                }
                return;
            }
            Overlay::Model(p) => {
                match p.key(k) {
                    Picked::Close => self.close_overlay(),
                    Picked::Choose(v) => {
                        self.overlay = Overlay::None;
                        self.set_model(if v.is_empty() { None } else { Some(v) });
                        self.next_ask();
                    }
                    Picked::None => {}
                }
                return;
            }
            Overlay::Sessions(p) => {
                match p.key(k) {
                    Picked::Close => self.close_overlay(),
                    Picked::Choose(v) => {
                        self.overlay = Overlay::None;
                        self.resume_choice(&v);
                        self.next_ask();
                    }
                    Picked::None => {}
                }
                return;
            }
            Overlay::Crew(v) => {
                match v.key(k, &self.crew) {
                    ViewAction::Close => self.overlay = Overlay::None,
                    ViewAction::Stop(id) => {
                        self.to_session("stop", |s| Up::StopTask { session: s, task_id: id });
                        self.brief("stopping that organ", false);
                    }
                    ViewAction::None => {}
                }
                return;
            }
            Overlay::None => {}
        }
        let (ctrl, shift) = (k.modifiers.contains(KeyModifiers::CONTROL), k.modifiers.contains(KeyModifiers::SHIFT));
        if self.t.sel.has() {
            // Ctrl-C (and Ctrl-Shift-C) copy a selection instead of counting towards quit; any other key drops it
            let copy = matches!(k.code, KeyCode::Char('c' | 'C')) && ctrl;
            let text = self.t.selected_text();
            self.t.sel.clear();
            self.dirty = true;
            if k.code == KeyCode::Esc {
                return; // Esc with a selection only clears it, visible text or not: never an interrupt
            }
            // a selection with nothing visible in it (off-screen, blank) swallows no Ctrl-C
            if let Some(text) = text {
                if copy {
                    self.effects.push(Effect::Copy(text));
                    self.brief("copied", false);
                    return;
                }
            }
        }
        match k.code {
            KeyCode::Char('o') if ctrl => self.t.toggle_cards(),
            KeyCode::Char('g') if ctrl => self.cycle_crew_view(),
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
                        self.brief("press Ctrl-C again to quit", false);
                    }
                }
                Action::CtrlD => self.quit = true,
                Action::Paste => {
                    // wl-paste/xclip can hang: ask on a worker, the text arrives as Msg::Clipboard
                    if let Some(post) = self.post.clone() {
                        std::thread::spawn(move || {
                            let _ = post.send(Msg::Clipboard(crate::util::system_clipboard()));
                        });
                    }
                }
            },
        }
    }

    pub fn submit(&mut self, text: String) {
        self.t.drop_transient();
        self.t.drop_face();
        if let Some(f) = &mut self.face {
            f.hide_full();
        }
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
                if a == "view" || a == "panel" || a == "log" {
                    return self.cycle_crew_view();
                }
                if a != "on" && a != "off" {
                    return self.notice(&format!("crew is {}; usage: /crew on|off|view", if self.settings.crew { "on" } else { "off" }), !a.is_empty());
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
                    self.brief(&format!("copied last reply ({n} chars)"), false);
                }
                _ => self.notice("nothing to copy (last reply)", true),
            },
            "/face" => match &mut self.face {
                // like Python: a full face joins the conversation and goes with the next message
                Some(f) => {
                    f.show_full(Instant::now());
                    self.t.push_face();
                    self.sync_face();
                    self.dirty = true;
                }
                None => self.notice("RaBbLE", false),
            },
            "/reload" => {
                if matches!(self.overlay, Overlay::Permission(_)) || !self.asks.is_empty() {
                    self.notice("reload when the approval is answered", true);
                } else if self.session.is_none() {
                    self.notice("reload: no engine session yet (/clear starts one)", true);
                } else {
                    self.reload = true;
                    self.quit = true;
                }
            }
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

    fn open_session_picker(&mut self, recent: Vec<sessions::Row>) {
        let now = SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs());
        let mut items: Vec<(String, String)> = self
            .live
            .iter()
            .filter(|s| Some(&s.session) != self.session.as_ref() && s.cwd == self.cfg.cwd)
            .map(|s| (format!("running {} · {} client(s){}", s.session, s.clients, if s.busy { " · busy" } else { "" }), format!("attach:{}", s.session)))
            .collect();
        let running: HashSet<_> = self.live.iter().filter_map(|s| s.sdk_session.clone()).collect();
        items.extend(
            recent.into_iter().filter(|r| !running.contains(&r.id)).map(|r| (format!("{:<70} {}", r.title, sessions::ago(r.updated, now)), format!("resume:{}", r.id))),
        );
        self.overlay = Overlay::Sessions(Picker::new("Resume a session", items, true, "No earlier sessions here", None));
    }

    fn resume_choice(&mut self, v: &str) {
        self.asks.clear(); // asks belong to the session we are leaving: never answer them against another
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
        // the header grows to the mark's six rows when the pane is big enough for it
        let has_face = self.face.is_some();
        let face_on = has_face && a.width >= FACE_MIN_AREA.0 && a.height >= FACE_MIN_AREA.1;
        // smaller panes keep a compact mark in a three-row header; tiny ones drop it (and the third line) for two text rows
        let compact = has_face && !face_on && a.width >= FACE_COMPACT_MIN_AREA.0 && a.height >= FACE_COMPACT_MIN_AREA.1;
        let hh = if face_on { FACE_ROWS } else if compact { FACE_COMPACT_ROWS } else { 2 };
        let (mark_w, mark_h) = if face_on { (FACE_SLOT_WIDTH, FACE_ROWS) } else { (FACE_COMPACT_SLOT_WIDTH, FACE_COMPACT_ROWS) };
        self.last_width = a.width;
        let top = hh + 1;
        let comp_h = self.composer.height(a.width).min(a.height.saturating_sub(top + 1));
        let tr_h = a.height.saturating_sub(top + comp_h);
        self.header.session = self.session.clone().unwrap_or_default();
        let phase = self.flow_t0.elapsed().as_secs_f64() * crate::theme::FLOW_SPEED;
        if self.header.usage.is_none() {
            self.header.usage = Some(crate::usage::read());
            self.usage_at = Some(Instant::now());
        }
        let (ha, da) = (Rect { height: hh, ..a }, Rect { y: a.y + hh, height: 1, ..a });
        let mut ta = Rect { y: a.y + top, height: tr_h, ..a };
        // the crew panel takes the right edge of the transcript's rows
        let panel_on = self.crew_panel && matches!(self.overlay, Overlay::None | Overlay::Permission(_)) && a.width >= PANEL_MIN_AREA;
        let pa = Rect { x: (a.x + a.width).saturating_sub(PANEL_WIDTH), width: PANEL_WIDTH.min(a.width), ..ta };
        if panel_on {
            ta.width = a.width.saturating_sub(PANEL_WIDTH + 1);
        }
        self.tr_rect = ta;
        let ca = Rect { y: a.y + top + tr_h, height: comp_h, ..a };
        self.t.layout(&self.th, ta.width, tr_h);
        self.gz.newest = self.t.newest_line();
        let buf = f.buffer_mut();
        // the micro mark is all eye, so its text keeps one more column of air than the full mark's
        let text_slot = if face_on { mark_w } else if compact { mark_w + 1 } else { 0 };
        self.header.draw(ha, buf, &self.th, text_slot, face_on, phase);
        draw_divider(da, buf, &self.th, self.busy_label(), phase);
        ratatui::widgets::Widget::render(View(&self.t), ta, buf);
        if panel_on {
            crate::widgets::crew::panel(&self.crew, pa, buf, &self.th, self.turn_open);
        }
        self.gz.centres = [None, None];
        self.gz.modal = None;
        if let Some(hub) = &mut self.face {
            hub.mini_visible = face_on || compact;
            if face_on || compact {
                // the mark is rebuilt only when the tier flips; a refit must not paint one blank frame
                let flipped = hub.mini.set_micro(!face_on);
                if hub.mini.fit(mark_w as usize, mark_h as usize) || flipped {
                    hub.mini.tick(Instant::now());
                }
                self.gz.centres[0] = Some((ha.x as f64 + mark_w.min(a.width) as f64 / 2.0, ha.y as f64 + mark_h as f64 / 2.0));
                hub.mini.draw(Rect { width: mark_w.min(a.width), height: mark_h, ..ha }, buf, &self.th, 0);
            }
            // the full face (`/face`) fills the room reserved for it in the transcript
            if !self.t.has_face() {
                hub.hide_full();
            }
            hub.full_visible = false;
            if let (Some(full), Some((rect, skip))) = (&mut hub.full, self.t.face_rect(ta)) {
                if full.fit(a.width as usize, self.t.face_rows()) {
                    full.tick(Instant::now()); // a refit (composer grew, window resized) must not paint one blank frame
                }
                hub.full_visible = true;
                let (cols, rows) = full.size();
                self.gz.centres[1] = Some((rect.x as f64 + cols as f64 / 2.0, rect.y as f64 - skip as f64 + rows as f64 / 2.0));
                full.draw(rect, buf, &self.th, skip);
            }
        }
        let focused = matches!(self.overlay, Overlay::None);
        let cursor = self.composer.draw(ca, buf, &self.th, focused, phase);
        self.gz.cursor = cursor;
        if matches!(self.overlay, Overlay::Permission(_)) {
            let m = Rect { y: a.y + top, height: a.height - top, ..a };
            self.gz.modal = Some((m.x as f64 + m.width as f64 / 2.0, m.y as f64 + m.height as f64 / 2.0));
        }
        if self.composer.completing() && focused {
            let n = self.composer.dropdown.len().min(8) as u16;
            let drop = Rect { y: ca.y.saturating_sub(n).max(ta.y), height: n.min(tr_h), ..a };
            self.composer.draw_dropdown(drop, buf, &self.th);
        }
        match &self.overlay {
            Overlay::Permission(p) => p.draw(Rect { y: a.y + top, height: a.height - top, ..a }, buf, &self.th),
            Overlay::Model(p) | Overlay::Sessions(p) => p.draw(a, buf, &self.th),
            Overlay::Crew(v) => {
                v.draw(&self.crew, Rect { y: a.y + top, height: tr_h, ..a }, buf, &self.th, self.turn_open);
                f.set_cursor_position(cursor);
            }
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

    fn ctrl(c: char) -> Event {
        Event::Key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::CONTROL))
    }

    #[test]
    fn organ_events_fill_the_crew_and_ctrl_g_walks_panel_viewer_hidden() {
        let (mut a, rx) = live();
        ev(&mut a, r#"{"type":"OrganStarted","organ":"gobble","tool_use_id":"u","task_id":"k9","brief":"map it"}"#);
        ev(&mut a, r#"{"type":"ToolCall","organ":"gobble","id":"t1","name":"Grep","input":{"pattern":"x"}}"#);
        assert_eq!((a.crew.active(), a.crew.get("gobble").unwrap().last_tool.as_str()), (1, "Grep x"));
        let wide = screen(&mut a, 120, 30);
        assert!(!wide.contains("FLOW") && !a.crew_panel, "hidden until asked");
        a.on_input(ctrl('g'));
        let s = screen(&mut a, 120, 30);
        assert!(a.crew_panel && s.contains("CREW  1 working") && s.contains("map it"), "{s}");
        a.on_input(ctrl('g'));
        assert!(matches!(a.overlay, Overlay::Crew(_)) && !a.crew_panel);
        assert!(screen(&mut a, 120, 30).contains("Grep x"));
        // x stops the selected, running organ
        a.on_input(key(KeyCode::Char('x')));
        let out = sent(&rx);
        assert!(out.iter().any(|m| m["type"] == "stop_task" && m["task_id"] == "k9"), "{out:?}");
        a.on_input(ctrl('g'));
        assert!(matches!(a.overlay, Overlay::None));
    }

    #[test]
    fn an_approval_pulls_the_reader_out_of_the_crew_viewer_and_a_narrow_pane_skips_the_panel() {
        let (mut a, _rx) = live();
        let _ = screen(&mut a, 70, 24);
        a.on_input(ctrl('g'));
        assert!(matches!(a.overlay, Overlay::Crew(_)), "narrow: straight to the viewer");
        ev(&mut a, &ask("a1"));
        assert!(matches!(&a.overlay, Overlay::Permission(p) if p.ask.id == "a1"));
        assert_eq!(a.crew.waiting(), 0, "the lead's own ask does not make an organ wait");
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
        let (ptx, prx) = std::sync::mpsc::channel();
        a.post = Some(ptx);
        a.on_net(Net::Reply { label: "resume-list".into(), frame: f });
        // the history scan runs on a worker and arrives as a message
        let m = prx.recv_timeout(std::time::Duration::from_secs(5)).expect("worker result");
        a.on_msg(m);
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

    fn with_post(a: &mut App) -> std::sync::mpsc::Receiver<Msg> {
        let (ptx, prx) = std::sync::mpsc::channel();
        a.post = Some(ptx);
        prx
    }
    fn arm(a: &mut App) {
        if let Overlay::Permission(p) = &mut a.overlay {
            p.armed_at = Instant::now() - Duration::from_secs(1);
        }
    }

    #[test]
    fn held_keys_cannot_answer_a_prompt_but_still_repeat_in_the_composer() {
        let (mut a, rx) = live();
        ev(&mut a, &ask("a1"));
        arm(&mut a);
        let mut k = KeyEvent::new(KeyCode::Char('y'), KeyModifiers::NONE);
        k.kind = KeyEventKind::Repeat;
        a.on_input(Event::Key(k));
        assert!(sent(&rx).is_empty() && matches!(a.overlay, Overlay::Permission(_)));
        let (mut b, _) = live();
        b.on_input(Event::Key(k));
        b.on_input(Event::Key(k));
        assert_eq!(b.composer.text(), "yy");
    }

    #[test]
    fn typing_at_60ms_past_the_window_does_not_answer_through_the_app() {
        let (mut a, rx) = live();
        ev(&mut a, &ask("a1"));
        // the app path uses the real clock: shove the deadline into the past, then type with the
        // virtual clock directly on the prompt (the unit test for the timing is in modal.rs)
        if let Overlay::Permission(p) = &mut a.overlay {
            let t0 = Instant::now();
            for i in 0..12u64 {
                assert_eq!(p.key_at(key_ev('y'), t0 + Duration::from_millis(60 * i)), None);
            }
        }
        assert!(sent(&rx).is_empty());
    }
    fn key_ev(c: char) -> KeyEvent {
        KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)
    }

    #[test]
    fn an_ask_that_arrives_while_a_picker_is_open_appears_when_it_closes() {
        let (mut a, _rx) = live();
        say(&mut a, "/model");
        ev(&mut a, &ask("a1"));
        assert!(matches!(a.overlay, Overlay::Model(_)), "the picker keeps the screen");
        assert_eq!(a.busy_label(), "awaiting approval");
        a.on_input(key(KeyCode::Esc));
        assert!(matches!(&a.overlay, Overlay::Permission(p) if p.ask.id == "a1"));
        // and the same after choosing from it
        let (mut b, _rx) = live();
        say(&mut b, "/model");
        ev(&mut b, &ask("a2"));
        b.on_input(key(KeyCode::Enter));
        assert!(matches!(&b.overlay, Overlay::Permission(p) if p.ask.id == "a2"));
    }

    #[test]
    fn a_late_session_list_never_replaces_a_prompt_and_switching_clears_queued_asks() {
        let (mut a, rx) = live();
        ev(&mut a, &ask("a1"));
        a.on_msg(Msg::Sessions(vec![]));
        assert!(matches!(&a.overlay, Overlay::Permission(p) if p.ask.id == "a1"), "prompt kept");
        assert!(screen(&mut a, 100, 30).contains("run /resume again"));
        // queued ask from the old session is dropped when the reader moves to another session
        let (mut b, rx2) = live();
        say(&mut b, "/model");
        ev(&mut b, &ask("old"));
        b.overlay = Overlay::Sessions(Picker::new("R", vec![("x".into(), "attach:s2".into())], true, "", None));
        b.on_input(key(KeyCode::Enter));
        assert!(matches!(b.overlay, Overlay::None), "the old session's ask must not open against s2");
        assert_eq!(b.session.as_deref(), Some("s2"));
        assert!(sent(&rx2).iter().all(|m| m["type"] != "answer"));
        let _ = rx;
    }

    #[test]
    fn a_replaced_creation_is_closed_not_adopted() {
        let (mut a, rx) = app_with(Start::New { resume: None });
        feed(&mut a, r#"{"type":"hello","version":1,"engine":"e","user":"u"}"#);
        let first = sent(&rx)[0]["id"].as_str().unwrap().to_owned();
        say(&mut a, "/clear");
        let second = sent(&rx).iter().find(|m| m["type"] == "create_session").unwrap()["id"].as_str().unwrap().to_owned();
        let reply = |id: &str, s: &str| Net::Reply { label: "create".into(), frame: Frame::parse(&format!(r#"{{"type":"session_created","session":"{s}","id":"{id}"}}"#)).unwrap() };
        a.on_net(reply(&first, "stale"));
        assert!(a.session.is_none());
        assert!(sent(&rx).iter().any(|m| m["type"] == "close_session" && m["session"] == "stale"));
        a.on_net(reply(&second, "fresh"));
        assert_eq!(a.session.as_deref(), Some("fresh"));
    }

    #[test]
    fn an_error_from_another_session_is_not_shown() {
        let (mut a, _rx) = live();
        feed(&mut a, r#"{"type":"Error","session":"zzz","text":"not yours"}"#);
        feed(&mut a, r#"{"type":"Error","text":"engine level"}"#);
        let s = screen(&mut a, 100, 20);
        assert!(!s.contains("not yours") && s.contains("engine level"), "{s}");
    }

    #[test]
    fn workers_post_back_files_clipboard_and_quit() {
        let dir = std::env::temp_dir().join(format!("rt-appfiles-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("notes.md"), "x").unwrap();
        let (mut a, _rx) = live();
        a.composer = Composer::new(dir.to_str().unwrap(), vec![]);
        let prx = with_post(&mut a);
        type_in(&mut a, "see @not");
        assert!(a.composer.dropdown.is_empty());
        let m = prx.recv_timeout(Duration::from_secs(5)).expect("scan result");
        assert!(matches!(m, Msg::Files(_)));
        a.on_msg(m);
        assert_eq!(a.composer.dropdown, ["notes.md"]);
        a.on_msg(Msg::Clipboard("pasted".into()));
        assert!(a.composer.text().ends_with("pasted"));
        a.on_msg(Msg::Quit("terminated (signal 15)".into()));
        assert!(a.quit && a.quit_reason.as_deref() == Some("terminated (signal 15)"));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_dropped_connection_reconnects_and_reattaches_from_the_last_seq() {
        let (mut a, _rx) = live();
        let prx = with_post(&mut a);
        a.started = true; // the first hello already created the session
        let (h2, rx2) = Handle::detached();
        let h2cell = std::sync::Mutex::new(Some(h2));
        a.connector = Some(Arc::new(move || Ok(h2cell.lock().unwrap().take().expect("one reconnect"))));
        ev(&mut a, r#"{"type":"TextDelta","seq":41,"organ":"rabble","text":"x"}"#);
        feed(&mut a, r#"{"type":"TextDelta","session":"s1","seq":42,"organ":"rabble","text":"y"}"#);
        a.on_net(Net::Closed("eof".into()));
        assert!(!a.engine_ok && screen(&mut a, 100, 20).contains("reconnecting"));
        let m = prx.recv_timeout(Duration::from_secs(5)).expect("reconnected");
        assert!(matches!(m, Msg::Reconnected(_)));
        a.on_msg(m);
        assert!(a.engine_ok && a.header.connected);
        feed(&mut a, r#"{"type":"hello","version":1,"engine":"e","user":"u"}"#);
        let s = sent(&rx2);
        let at = s.iter().find(|m| m["type"] == "attach").expect("attach");
        assert_eq!((at["session"].as_str(), at["since_seq"].as_u64()), (Some("s1"), Some(42)));
        // a second hello does not attach again
        feed(&mut a, r#"{"type":"hello","version":1,"engine":"e","user":"u"}"#);
        assert!(sent(&rx2).iter().all(|m| m["type"] != "attach"));
    }

    // ---- the face ----------------------------------------------------------------------------
    fn face_app() -> (App, Receiver<String>) {
        let pal = rabble_theme::Palette::parse(
            "  bg: \"#0a0010\"\n  surface: \"#101018\"\n  text: \"#e8e6f0\"\n  muted: \"#6b6880\"\n  magenta: \"#ff2bd6\"\n  cyan: \"#00f5ff\"\n  violet: \"#8a4dff\"\n  pink: \"#ff7ab8\"\n",
        );
        let (h, rx) = Handle::detached();
        let cfg = Config { cwd: "/w".into(), start: Start::New { resume: None }, client: CLIENT.into() };
        let mut a = App::new(cfg, Theme::new(Some(&pal), false, Glyphs::FANCY), Settings::default(), h);
        a.session = Some("s1".into());
        (a, rx)
    }
    /// `/face`, with the cell shape pinned so the tests do not depend on the developer's terminal.
    fn show_face(a: &mut App) {
        say(a, "/face");
        a.face.as_mut().unwrap().full.as_mut().unwrap().set_cell_aspect(2.0);
    }
    fn mood(a: &App) -> Mood {
        a.face.as_ref().unwrap().mood()
    }
    fn paint_face(a: &mut App, w: u16, h: u16) -> ratatui::buffer::Buffer {
        let mut t = Terminal::new(TestBackend::new(w, h)).unwrap();
        t.draw(|f| a.draw(f)).unwrap();
        a.tick_face(Instant::now() + Duration::from_millis(500));
        t.draw(|f| a.draw(f)).unwrap();
        t.backend().buffer().clone()
    }

    #[test]
    fn the_mark_takes_six_header_rows_in_a_big_pane_and_none_in_a_small_one() {
        let (mut a, _rx) = face_app();
        let big = paint_face(&mut a, 100, 30);
        let mark_cells = (0..24).flat_map(|x| (0..6).map(move |y| (x, y))).filter(|&(x, y)| big[(x, y)].symbol() != " ").count();
        assert!(mark_cells > 20, "the mark is drawn in the slot: {mark_cells} cells");
        let header: String = (0..60).map(|x| big[(25 + x, 0)].symbol().to_owned()).collect();
        assert!(header.contains("RaBbLE"), "the wordmark leads the info lines beside the mark: {header:?}");
        let row = |y: u16| -> String { (0..60).map(|x| big[(25 + x, y)].symbol().to_owned()).collect() };
        assert!(row(2).contains("capability") && row(4).contains("usage") && row(5).contains("API equiv"), "Python's six info lines: {:?}", (2..6).map(row).collect::<Vec<_>>());
        let (mut b, _rx) = face_app();
        let tiny = paint_face(&mut b, 28, 18);
        assert!(tiny[(0, 0)].symbol() == "R", "a tiny pane keeps the plain two-row header: {:?}", tiny[(0, 0)].symbol());
        assert_eq!(b.face_due(), None, "a hidden mark costs nothing");
        let (mut c, _rx) = face_app();
        let short = paint_face(&mut c, 80, 9);
        assert!(short[(0, 0)].symbol() == "R" && c.face_due().is_none(), "too short for even the compact mark");
    }

    fn mark_cells(buf: &ratatui::buffer::Buffer, w: u16, h: u16) -> usize {
        (0..w).flat_map(|x| (0..h).map(move |y| (x, y))).filter(|&(x, y)| buf[(x, y)].symbol() != " ").count()
    }

    #[test]
    fn a_small_pane_gets_the_compact_mark_and_the_full_tier_is_unchanged() {
        let (mut a, _rx) = face_app();
        let small = paint_face(&mut a, 40, 12);
        assert!(mark_cells(&small, 10, 3) > 4, "compact mark in the 10x3 slot");
        let row: String = (0..25).map(|x| small[(12 + x, 0)].symbol().to_owned()).collect();
        assert!(row.starts_with("RaBbLE"), "text reflows beside it with two columns of air: {row:?}");
        assert!(a.face_due().is_some());
        assert_eq!(a.gz.centres[0], Some((5.0, 1.5)), "gaze centre follows the compact slot");
        assert_eq!(a.tr_rect.y, 4, "three header rows + divider");
        let usage: String = (0..25).map(|x| small[(12 + x, 2)].symbol().to_owned()).collect();
        assert!(usage.starts_with("usage"), "the third header line is usage: {usage:?}");
        let (mut b, _rx) = face_app();
        let edge = paint_face(&mut b, 30, 10);
        assert!(mark_cells(&edge, 10, 3) > 4, "minimum compact pane");
        let (mut c, _rx) = face_app();
        let big = paint_face(&mut c, 100, 40);
        assert!(mark_cells(&big, 24, 6) > 20 && c.gz.centres[0] == Some((12.0, 3.0)) && c.tr_rect.y == 7);
        // dump for the report
        for y in 0..3 { eprintln!("COMPACT|{}", (0..40).map(|x| small[(x, y)].symbol().to_owned()).collect::<String>()); }
    }

    #[test]
    fn reload_quits_for_a_reexec_unless_an_approval_is_open_or_there_is_no_session() {
        let (mut a, _rx) = live();
        say(&mut a, "/reload");
        assert!(a.quit && a.reload);
        let (mut b, _rx) = app_with(Start::New { resume: None });
        say(&mut b, "/reload");
        assert!(!b.quit && !b.reload, "no session to reattach to");
        let (mut c, _rx) = live();
        ev(&mut c, r#"{"type":"PermissionAsk","organ":"rabble","id":"p1","tool":"Bash","input":{"command":"ls"}}"#);
        say(&mut c, "/reload");
        assert!(!c.quit && !c.reload, "a pending approval blocks it");
    }

    #[test]
    fn a_restart_forgets_the_old_sessions_tools_and_stream() {
        let (mut a, _rx) = face_app();
        ev(&mut a, r#"{"type":"TextDelta","organ":"rabble","text":"hi"}"#);
        ev(&mut a, r#"{"type":"ToolCall","organ":"rabble","id":"t1","name":"Bash","input":{"command":"ls"}}"#);
        assert_eq!(mood(&a), Mood::Working);
        say(&mut a, "/clear");
        assert!(!a.streaming && a.busy.is_empty());
        assert_eq!(mood(&a), Mood::Idle, "a fresh session does not inherit the old one's work");
    }

    #[test]
    fn without_colour_usage_still_refreshes_and_the_face_costs_nothing() {
        let pal = rabble_theme::Palette::parse(
            "  bg: \"#0a0010\"\n  surface: \"#101018\"\n  text: \"#e8e6f0\"\n  muted: \"#6b6880\"\n  magenta: \"#ff2bd6\"\n  cyan: \"#00f5ff\"\n  violet: \"#8a4dff\"\n  pink: \"#ff7ab8\"\n",
        );
        let (h, _rx) = Handle::detached();
        let cfg = Config { cwd: "/w".into(), start: Start::New { resume: None }, client: CLIENT.into() };
        let mut a = App::new(cfg, Theme::new(Some(&pal), true, Glyphs::FANCY), Settings::default(), h);
        assert_eq!(a.face_due(), None, "a static word needs no frames");
        let frames = a.face.as_ref().unwrap().mini.frames;
        a.tick_face(Instant::now() + Duration::from_secs(5));
        assert_eq!(a.face.as_ref().unwrap().mini.frames, frames);
        let due = a.flow_due().expect("usage still has a wake time");
        a.dirty = false;
        a.tick_flow(due + Duration::from_secs(31));
        assert!(a.dirty && a.header.usage.is_some(), "usage is re-read and repainted with no colour");
    }

    #[test]
    fn a_refit_face_never_paints_blank() {
        let (mut a, _rx) = face_app();
        show_face(&mut a);
        let mut t = Terminal::new(TestBackend::new(100, 40)).unwrap();
        t.draw(|f| a.draw(f)).unwrap();
        type_in(&mut a, "x");
        a.composer.insert_str("\n\n\nmore"); // the composer grows, the transcript shrinks, the face refits
        t.draw(|f| a.draw(f)).unwrap();
        assert!(!a.hub_full_cells_empty(), "cells are ready on the very paint that refit");
    }

    #[test]
    fn dragging_selects_transcript_text_and_ctrl_c_copies_it() {
        use crossterm::event::{MouseButton::Left, MouseEventKind::{Down, Drag, Up}};
        let (mut a, _rx) = live();
        ev(&mut a, r#"{"type":"TextBlock","organ":"rabble","text":"alpha beta\n\ngamma delta"}"#);
        let scr = screen(&mut a, 60, 20);
        let row = scr.lines().position(|l| l.contains("alpha beta")).expect("reply painted") as u16;
        let m = |kind, column, row| Event::Mouse(MouseEvent { kind, column, row, modifiers: KeyModifiers::NONE });
        let c = a.tr_rect.x + scr.lines().nth(row as usize).unwrap().find("alpha").unwrap() as u16;
        a.on_input(m(Down(Left), c, row));
        a.on_input(m(Drag(Left), c + 9, row));
        a.on_input(m(Up(Left), c + 9, row));
        assert!(a.t.sel.has());
        let ctrl_c = Event::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL));
        a.on_input(ctrl_c.clone());
        assert_eq!(a.effects, [Effect::Copy("alpha beta".into())]);
        assert!(!a.t.sel.has() && !a.quit, "a copy is not a quit hint");
        // double click takes the word
        let scr = screen(&mut a, 60, 20);
        let row = scr.lines().position(|l| l.contains("alpha beta")).unwrap() as u16;
        let c2 = a.tr_rect.x + scr.lines().nth(row as usize).unwrap().find("beta").unwrap() as u16;
        a.on_input(m(Down(Left), c2, row));
        a.on_input(m(Up(Left), c2, row));
        a.on_input(m(Down(Left), c2, row));
        assert_eq!(a.t.selected_text().as_deref(), Some("beta"));
        // typing drops the selection and Ctrl-C then behaves as before
        a.on_input(key(KeyCode::Char('x')));
        assert!(!a.t.sel.has());
        a.effects.clear();
        a.on_input(ctrl_c);
        assert!(a.effects.is_empty());
    }

    #[test]
    fn clear_forgets_the_old_sessions_entropy_everywhere() {
        let (mut a, _rx) = face_app();
        ev(&mut a, r#"{"type":"EntropyChanged","score":0.8,"band":"DEGRADED","trust":"strict","reason":"x","capability":30,"categories":{"general":30},"local":false,"base":30,"stability":20}"#);
        assert!(a.header.entropy.is_some() && a.header.stability == Some(20) && a.header.capability == Some(30));
        say(&mut a, "/clear");
        assert!(a.header.entropy.is_none() && a.header.stability.is_none() && a.header.capability.is_none() && a.header.categories.is_empty());
        assert!(a.trust_profile().is_empty());
        assert!(a.face.as_ref().unwrap().signal_is_clear());
    }

    #[test]
    fn pointer_motion_neither_repaints_nor_holds_back_the_face() {
        use crossterm::event::MouseEventKind::{Moved, ScrollDown};
        let (mut a, _rx) = face_app();
        a.dirty = false;
        let at = |kind| Event::Mouse(MouseEvent { kind, column: 3, row: 3, modifiers: KeyModifiers::NONE });
        a.on_input(at(Moved));
        assert!(!a.dirty, "a mouse move changes nothing on screen");
        a.on_input(at(ScrollDown));
        assert!(a.dirty);
    }

    #[test]
    fn divider_and_input_border_flow_between_cyan_and_magenta() {
        let (mut a, _rx) = face_app();
        let buf = paint_face(&mut a, 100, 30);
        let fg = |x: u16, y: u16| buf[(x, y)].style().fg;
        let divider: std::collections::HashSet<_> = (0..100).map(|x| fg(x, 6)).collect();
        assert!(divider.len() > 10 && !divider.contains(&None), "the divider is a gradient: {} colours", divider.len());
        let top = 30 - a.composer.height(100);
        assert!((0..100).map(|x| fg(x, top)).collect::<std::collections::HashSet<_>>().len() > 10, "the input border is a gradient");
        assert!(a.flow_due().is_some());
    }

    #[test]
    fn mood_follows_the_events_the_python_widget_reacts_to() {
        let (mut a, _rx) = face_app();
        assert_eq!(mood(&a), Mood::Idle);
        ev(&mut a, r#"{"type":"TextDelta","organ":"rabble","text":"hi"}"#);
        assert_eq!(mood(&a), Mood::Streaming);
        ev(&mut a, r#"{"type":"ToolCall","organ":"rabble","id":"t1","name":"Bash","input":{"command":"ls"}}"#);
        assert_eq!(mood(&a), Mood::Working);
        ev(&mut a, r#"{"type":"ToolResult","organ":"rabble","id":"t1","ok":true,"summary":"ok"}"#);
        assert_eq!(mood(&a), Mood::Idle);
        type_in(&mut a, "hel");
        assert_eq!(mood(&a), Mood::Typing);
        ev(&mut a, r#"{"type":"Error","text":"boom"}"#);
        assert_eq!(mood(&a), Mood::Error, "an error flashes");
        a.flash = Some((Mood::Error, Instant::now() - Duration::from_millis(1)));
        a.sync_face();
        assert_eq!(mood(&a), Mood::Typing, "and the flash ends");
        ev(&mut a, r#"{"type":"OrganStarted","organ":"gobble","tool_use_id":"u","task_id":"k","brief":"x"}"#);
        a.composer.set_text("");
        a.sync_face();
        assert_eq!(mood(&a), Mood::Working);
        ev(&mut a, r#"{"type":"TurnDone","stop_reason":"end_turn","origin":"observer"}"#);
        assert_eq!(mood(&a), Mood::Idle, "an observer turn ending clears what was running");
    }

    #[test]
    fn denying_a_permission_flashes_the_denied_mood() {
        let (mut a, _rx) = face_app();
        ev(&mut a, &ask("a1"));
        a.on_input(key(KeyCode::Esc));
        assert_eq!(mood(&a), Mood::Denied);
    }

    #[test]
    fn the_providers_tint_follows_the_model_and_the_entropy_signal_reaches_the_face() {
        let (mut a, _rx) = face_app();
        let tint = |a: &App| a.face.as_ref().unwrap().mini_provider_for_test();
        assert_eq!(tint(&a), Some(rabble_face::Provider::Anthropic), "no model named = Claude Code's own default");
        ev(&mut a, r#"{"type":"ModelUsed","model":"gemini-3-pro"}"#);
        assert_eq!(tint(&a), Some(rabble_face::Provider::Google));
        ev(&mut a, r#"{"type":"EntropyChanged","score":0.4,"band":"NOMINAL","trust":"standard","base":35,"stability":40,"local":true}"#);
        assert_eq!(tint(&a), None, "a local model has no provider");
        assert!(a.face.as_ref().unwrap().mini_noise_for_test() > 0.3, "capability 35 is grainy");
    }

    #[test]
    fn slash_face_mounts_a_full_face_until_the_next_message() {
        let (mut a, rx) = face_app();
        show_face(&mut a);
        assert!(a.t.has_face() && a.face.as_ref().unwrap().full.is_some());
        assert!(sent(&rx).is_empty(), "/face is local");
        let before = paint_face(&mut a, 100, 40);
        let after = paint_face(&mut a, 100, 40);
        let full = a.face.as_ref().unwrap().full.as_ref().unwrap();
        assert_eq!(full.size(), (100, 40 - 7 - a.composer.height(100) as usize), "the face fills the transcript pane");
        let painted = (0..100).flat_map(|x| (8..30).map(move |y| (x, y))).filter(|&(x, y)| after[(x, y)].bg != ratatui::style::Color::Reset).count();
        assert!(painted > 100, "the full face paints its cells: {painted}");
        let _ = before;
        say(&mut a, "hello");
        assert!(!a.t.has_face() && a.face.as_ref().unwrap().full.is_none(), "the next submit drops it");
        assert!(sent(&rx).iter().any(|m| m["type"] == "send"));
    }

    #[test]
    fn keys_and_wheel_never_compute_a_face_frame() {
        let (mut a, _rx) = face_app();
        let _ = paint_face(&mut a, 100, 30);
        let frames = a.face.as_ref().unwrap().mini.frames;
        for c in "typing away".chars() {
            a.on_input(key(KeyCode::Char(c)));
        }
        a.on_input(Event::Mouse(MouseEvent { kind: MouseEventKind::ScrollUp, column: 3, row: 3, modifiers: KeyModifiers::NONE }));
        assert_eq!(a.face.as_ref().unwrap().mini.frames, frames, "only tick_face computes a frame");
    }

    #[test]
    fn a_face_frame_is_cheap_enough_to_sit_between_keys() {
        let (mut a, _rx) = face_app();
        show_face(&mut a);
        let _ = paint_face(&mut a, 220, 60);
        let mut now = Instant::now() + Duration::from_secs(1);
        let mut ms = Vec::new();
        for _ in 0..60 {
            now += Duration::from_millis(40);
            let t0 = Instant::now();
            a.tick_face(now);
            ms.push(t0.elapsed().as_secs_f64() * 1e3);
        }
        ms.sort_by(|a, b| a.partial_cmp(b).unwrap());
        eprintln!("mark + full face at 220x60, tick p50 {:.2} ms p99 {:.2} ms", ms[30], ms[58]);
        assert!(ms[58] < 8.0, "p99 {} ms", ms[58]);
    }

    #[test]
    fn no_face_without_a_palette() {
        let (a, _rx) = live();
        assert!(a.face.is_none() && a.face_due().is_none());
    }

    #[test]
    fn copy_mid_stream_keeps_one_reply() {
        use crossterm::event::{MouseButton::Left, MouseEventKind::{Down, Drag, Up}};
        let (mut a, _rx) = live();
        say(&mut a, "hi");
        ev(&mut a, r#"{"type":"TextDelta","organ":"rabble","text":"first half "}"#);
        let scr = screen(&mut a, 60, 20);
        let row = scr.lines().position(|l| l.contains("first half")).unwrap() as u16;
        let m = |kind, column, row| Event::Mouse(MouseEvent { kind, column, row, modifiers: KeyModifiers::NONE });
        let c = a.tr_rect.x + scr.lines().nth(row as usize).unwrap().find("first").unwrap() as u16;
        a.on_input(m(Down(Left), c, row));
        a.on_input(m(Drag(Left), c + 4, row));
        a.on_input(m(Up(Left), c + 4, row));
        a.on_input(Event::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)));
        assert_eq!(a.effects, [Effect::Copy("first".into())]);
        ev(&mut a, r#"{"type":"TextDelta","organ":"rabble","text":"second half"}"#);
        ev(&mut a, r#"{"type":"TextBlock","organ":"rabble","text":"first half second half"}"#);
        let out = screen(&mut a, 60, 20);
        assert_eq!(out.matches("first half second half").count(), 1, "{out}");
        assert!(!out.contains("first half \n"), "{out}");
    }

    #[test]
    fn a_drag_that_began_outside_the_transcript_does_nothing() {
        use crossterm::event::{MouseButton::Left, MouseEventKind::{Down, Drag}};
        let (mut a, _rx) = live();
        for i in 0..40 {
            ev(&mut a, &format!(r#"{{"type":"TextBlock","organ":"rabble","text":"line {i}"}}"#));
        }
        screen(&mut a, 60, 20);
        let m = |kind, column, row| Event::Mouse(MouseEvent { kind, column, row, modifiers: KeyModifiers::NONE });
        a.t.scroll_by(-5);
        let off = a.t.offset;
        a.dirty = false;
        a.on_input(m(Down(Left), 3, 19)); // the composer, below the transcript
        a.on_input(m(Drag(Left), 5, 0));
        a.on_input(m(Drag(Left), 5, 19));
        assert_eq!(a.t.offset, off);
        assert!(!a.t.sel.has() && !a.dirty, "no scroll, no selection, no repaint");
    }

    #[test]
    fn an_empty_selection_does_not_swallow_the_first_ctrl_c_or_esc() {
        let (mut a, _rx) = live();
        ev(&mut a, r#"{"type":"TextBlock","organ":"rabble","text":"alpha"}"#);
        screen(&mut a, 60, 20);
        let far = a.t.total_lines() + 50;
        a.t.sel.set((far, 0), (far, 3)); // off-screen / nothing there
        a.on_input(Event::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)));
        assert!(!a.t.sel.has() && a.effects.is_empty());
        assert!(a.last_ctrl_c.is_some(), "the press counted towards quit");
    }

    #[test]
    fn esc_with_a_blank_selection_clears_it_and_never_interrupts() {
        let (mut a, rx) = live();
        ev(&mut a, r#"{"type":"TextBlock","organ":"rabble","text":"alpha"}"#);
        screen(&mut a, 60, 20);
        let esc = || Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        let far = a.t.total_lines() + 50;
        a.t.sel.set((far, 0), (far, 3));
        a.on_input(esc());
        assert!(!a.t.sel.has());
        assert!(rx.try_iter().all(|l| !l.contains("interrupt")), "Esc on a selection is not an interrupt");
        a.on_input(esc());
        assert!(rx.try_iter().any(|l| l.contains("interrupt")), "with no selection Esc interrupts as before");
    }

    #[test]
    fn dragging_upward_across_replies_keeps_the_selection() {
        use crossterm::event::{MouseButton::Left, MouseEventKind::{Down, Drag, Up}};
        let (mut a, _rx) = live();
        ev(&mut a, r#"{"type":"TextBlock","organ":"rabble","text":"first reply"}"#);
        ev(&mut a, r#"{"type":"TextBlock","organ":"rabble","text":"second reply"}"#);
        let scr = screen(&mut a, 60, 20);
        let at = |w: &str| (scr.lines().position(|l| l.contains(w)).unwrap() as u16, a.tr_rect.x + scr.lines().find(|l| l.contains(w)).unwrap().find(w).unwrap() as u16);
        let (r2, c2) = at("second");
        let (r1, c1) = at("first");
        let m = |kind, column, row| Event::Mouse(MouseEvent { kind, column, row, modifiers: KeyModifiers::NONE });
        a.on_input(m(Down(Left), c2 + 5, r2));
        a.on_input(m(Drag(Left), c2 + 2, r2));
        screen(&mut a, 60, 20); // a layout while the head is still in the anchor's entry
        a.on_input(m(Drag(Left), c1, r1));
        screen(&mut a, 60, 20); // a layout between events must not drop the anchor
        assert!(a.t.sel.has() && a.t.sel.dragging());
        a.on_input(m(Drag(Left), c1, r1));
        a.on_input(m(Up(Left), c1, r1));
        let text = a.t.selected_text().expect("selected");
        assert!(text.contains("first reply") && text.contains("second"), "{text}");
    }

    #[test]
    fn dragging_upward_past_the_top_scrolls_and_keeps_selecting() {
        use crossterm::event::{MouseButton::Left, MouseEventKind::{Down, Drag, Up}};
        let (mut a, _rx) = live();
        for i in 0..40 {
            ev(&mut a, &format!(r#"{{"type":"TextBlock","organ":"rabble","text":"line {i}"}}"#));
        }
        let scr = screen(&mut a, 60, 20);
        let m = |kind, column, row| Event::Mouse(MouseEvent { kind, column, row, modifiers: KeyModifiers::NONE });
        let row = a.tr_rect.y + a.tr_rect.height - 1;
        let c = a.tr_rect.x + 1;
        let _ = scr;
        let off = a.t.offset;
        a.on_input(m(Down(Left), c, row));
        for _ in 0..6 {
            a.on_input(m(Drag(Left), c, 0));
            screen(&mut a, 60, 20);
        }
        a.on_input(m(Up(Left), c, 0));
        assert!(a.t.offset + 4 <= off, "the view kept scrolling up: {} vs {off}", a.t.offset);
        assert!(a.t.sel.has());
        let n = a.t.selected_text().expect("copy is not empty").lines().count();
        assert!(n > 10, "{n} lines selected");
    }

    // ---- gaze ----
    #[test]
    fn the_chooser_has_one_answer_per_branch_and_a_priority() {
        use gaze::{choose, Focus, Scene};
        assert_eq!(choose(&Scene::default()), None, "nothing interesting: saccades");
        let base = Scene { cursor: (3.0, 4.0), newest: Some((9.0, 8.0)), ..Default::default() };
        assert_eq!(choose(&Scene { streaming: true, ..Scene { newest: base.newest, ..Default::default() } }), Some((Focus::Text, (9.0, 8.0))));
        assert_eq!(choose(&Scene { newest: base.newest, ..Default::default() }), None, "a finished reply is not interesting");
        assert_eq!(choose(&Scene { card: true, newest: base.newest, ..Default::default() }), Some((Focus::Card, (9.0, 8.0))));
        assert_eq!(choose(&Scene { typing: true, cursor: base.cursor, newest: base.newest, card: true, streaming: true, ..Default::default() }), Some((Focus::Typing, (3.0, 4.0))));
        assert_eq!(choose(&Scene { modal: Some((1.0, 1.0)), typing: true, card: true, ..Default::default() }), Some((Focus::Modal, (1.0, 1.0))));
    }

    #[test]
    fn attention_does_not_jitter_between_foci() {
        use gaze::{Focus::*, Steer, ATTN_HOLD};
        let (mut s, t0) = (Steer::default(), Instant::now());
        assert_eq!(s.pick(Some((Text, (1.0, 1.0))), t0), Some((1.0, 1.0)), "first focus is taken at once");
        assert_eq!(s.pick(Some((Text, (2.0, 2.0))), t0 + Duration::from_millis(100)), Some((2.0, 2.0)), "same focus tracks its target");
        assert_eq!(s.pick(Some((Typing, (5.0, 5.0))), t0 + Duration::from_millis(200)), Some((2.0, 2.0)), "a new focus waits");
        assert_eq!(s.pick(None, t0 + Duration::from_millis(300)), Some((2.0, 2.0)), "and so does losing interest");
        assert_eq!(s.pick(Some((Typing, (5.0, 5.0))), t0 + ATTN_HOLD + Duration::from_millis(1)), Some((5.0, 5.0)));
        assert_eq!(s.pick(None, t0 + ATTN_HOLD * 3), None, "after the hold it lets go");
    }

    #[test]
    fn aim_is_relative_to_the_face_and_clamped() {
        assert_eq!(gaze::aim((50.0, 10.0), (50.0, 10.0)), (0.0, 0.0));
        let (x, y) = gaze::aim((10.0, 30.0), (50.0, 10.0));
        assert!(x <= -1.0 + 1e-9 && y > 0.0 && y <= 1.0, "{x} {y}");
        let (x, _) = gaze::aim((60.0, 10.0), (50.0, 10.0));
        assert!(x > 0.0 && x < 1.0);
    }

    #[test]
    fn the_pointer_steers_both_faces_without_a_repaint_and_lapses() {
        use crossterm::event::MouseEventKind::Moved;
        let (mut a, _rx) = face_app();
        show_face(&mut a);
        a.gz.typed_at = None; // the command itself was typed
        screen(&mut a, 100, 40);
        let t0 = Instant::now();
        a.dirty = false;
        a.on_input(Event::Mouse(MouseEvent { kind: Moved, column: 2, row: 30, modifiers: KeyModifiers::NONE }));
        assert!(!a.dirty, "motion is not a repaint");
        assert!(a.gz.typed_at.is_none(), "motion is not typing");
        a.tick_face(t0 + Duration::from_millis(10));
        let [mini, full] = a.gz.aimed;
        let (mini, full) = (mini.expect("mini follows"), full.expect("a /face view follows too"));
        assert!(mini.0 < 0.0 && mini.1 > 0.0, "{mini:?}: the pointer is below-left of the mark");
        assert!(full.0 < 0.0);
        a.tick_face(t0 + gaze::POINTER_ACTIVE + Duration::from_millis(500));
        assert_eq!(a.gz.aimed, [None, None], "the pointer idled and nothing else is interesting: saccades");
    }

    #[test]
    fn a_face_opened_after_the_pointer_moved_gets_the_gaze() {
        use crossterm::event::MouseEventKind::Moved;
        let (mut a, _rx) = face_app();
        screen(&mut a, 100, 40);
        a.on_input(Event::Mouse(MouseEvent { kind: Moved, column: 90, row: 5, modifiers: KeyModifiers::NONE }));
        let t0 = Instant::now();
        a.tick_face(t0);
        assert!(a.gz.aimed[0].is_some() && a.gz.aimed[1].is_none());
        show_face(&mut a);
        screen(&mut a, 100, 40);
        a.tick_face(t0 + gaze::EVERY + Duration::from_millis(1));
        assert!(a.gz.aimed[1].is_some_and(|g| g.0 > 0.0), "re-applied on the next gaze tick");
    }

    #[test]
    fn the_entity_looks_at_streaming_text_and_the_composer_then_lets_go() {
        let (mut a, _rx) = face_app();
        // the run loop's order: tick the face, then paint (layout runs in paint)
        let t0 = Instant::now();
        screen(&mut a, 100, 40);
        a.tick_face(t0);
        assert_eq!(a.gz.aimed[0], None, "idle: random saccades");
        ev(&mut a, r#"{"type":"TextDelta","organ":"rabble","text":"hello there"}"#);
        screen(&mut a, 100, 40);
        a.tick_face(t0 + Duration::from_secs(1));
        assert!(a.gz.aimed[0].is_some_and(|g| g.1 > 0.0), "looks down at the newest line: {:?}", a.gz.aimed);
        // more text streams in; each delta makes the page stale and the tick runs before the paint
        for k in 1..=4u64 {
            ev(&mut a, r#"{"type":"TextDelta","organ":"rabble","text":" and more"}"#);
            a.tick_face(t0 + gaze::ATTN_HOLD + gaze::ATTN_EVERY * k as u32 + Duration::from_secs(k));
            screen(&mut a, 100, 40);
        }
        assert!(a.gz.aimed[0].is_some_and(|g| g.1 > 0.0), "still watching the stream past ATTN_HOLD: {:?}", a.gz.aimed);
        ev(&mut a, r#"{"type":"TextBlock","organ":"rabble","text":"hello there"}"#);
        type_in(&mut a, "x");
        let at = t0 + Duration::from_secs(20); // after the loop above, on the same virtual clock
        a.gz.typed_at = Some(at + gaze::ATTN_HOLD); // typed just before the tick below
        a.tick_face(at + gaze::ATTN_HOLD + Duration::from_millis(600));
        screen(&mut a, 100, 40);
        assert!(a.gz.aimed[0].is_some(), "typing keeps its interest");
        a.tick_face(at + gaze::TYPING_HOLD + gaze::ATTN_HOLD * 3);
        assert_eq!(a.gz.aimed[0], None, "nothing left to look at");
    }

    #[test]
    fn clear_resets_the_header_trust_label() {
        let (mut a, _rx) = face_app();
        ev(&mut a, r#"{"type":"EntropyChanged","score":0.8,"band":"DEGRADED","trust":"strict","reason":"x","capability":30,"categories":{"general":30},"local":false,"base":30,"stability":20}"#);
        assert_eq!(a.header.trust, "strict");
        say(&mut a, "/clear");
        assert_eq!(a.header.trust, a.trust_profile());
        assert_ne!(a.header.trust, "strict");
    }
}
