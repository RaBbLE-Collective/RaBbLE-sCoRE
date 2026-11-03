//! Modals: the permission prompt and the pickers (model, session). They own their keys and
//! return a result; the app owns what the result means. The permission prompt arms 0.4 s after it
//! opens so a keystroke meant for the composer cannot answer an ask nobody has read; Esc (deny)
//! is never held back.

use std::{cell::Cell, collections::BTreeMap, time::{Duration, Instant}};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use rabble_proto::Decision;
use ratatui::{buffer::Buffer, layout::Rect, text::{Line, Span}, widgets::Clear, widgets::Widget};
use serde_json::{Map, Value};

use crate::{theme::Theme, wrap::{self, clean}};

pub const ARM: Duration = Duration::from_millis(400);

#[derive(Debug, Clone, PartialEq)]
pub struct Ask {
    pub id: String,
    pub organ: String,
    pub tool: String,
    pub input: Map<String, Value>,
    pub title: Option<String>,
    pub description: Option<String>,
    pub blocked_path: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Reply {
    pub decision: Decision,
    pub reason: String,
    /// AskUserQuestion: question text → chosen label(s).
    pub answers: Option<BTreeMap<String, String>>,
}

/// One AskUserQuestion question as the shell walks it.
#[derive(Debug, Clone, PartialEq)]
struct Q {
    question: String,
    header: String,
    options: Vec<(String, String)>, // (label, description)
    multi: bool,
}

/// The AskUserQuestion walk: one question at a time; the last row of each is "Other" (typed).
#[derive(Debug, Clone, PartialEq)]
struct Qs {
    qs: Vec<Q>,
    at: usize,
    cursor: usize,
    checked: Vec<bool>, // multi-select ticks, options only
    other: Option<String>, // Some = typing a free answer
    done: Vec<String>,  // answers to qs[..at]
}

impl Qs {
    fn parse(i: &Map<String, Value>) -> Option<Qs> {
        let qs: Vec<Q> = i.get("questions")?.as_array()?.iter().filter_map(|q| {
            let q = q.as_object()?;
            let options = q.get("options").and_then(Value::as_array).map(|o| o.iter().filter_map(|o| match o {
                Value::String(l) => Some((l.clone(), String::new())),
                Value::Object(m) => Some((s(m, "label").to_owned(), s(m, "description").to_owned())),
                _ => None,
            }).collect()).unwrap_or_default();
            Some(Q { question: s(q, "question").to_owned(), header: s(q, "header").to_owned(), options, multi: q.get("multiSelect").and_then(Value::as_bool).unwrap_or(false) })
        }).filter(|q| !q.question.is_empty()).collect();
        (!qs.is_empty()).then(|| Qs { checked: vec![false; qs[0].options.len()], qs, at: 0, cursor: 0, other: None, done: Vec::new() })
    }

    fn rows(&self) -> usize {
        self.qs[self.at].options.len() + 1 // + Other
    }

    /// Record an answer and move on; Some when the last question is answered.
    fn finish(&mut self, ans: String) -> Option<Reply> {
        self.done.push(ans);
        self.at += 1;
        self.cursor = 0;
        self.other = None;
        if self.at < self.qs.len() {
            self.checked = vec![false; self.qs[self.at].options.len()];
            return None;
        }
        let answers = self.qs.iter().map(|q| q.question.clone()).zip(self.done.iter().cloned()).collect();
        Some(Reply { decision: Decision::Allow, reason: String::new(), answers: Some(answers) })
    }

    fn key(&mut self, k: KeyEvent) -> Option<Reply> {
        let q = self.qs[self.at].clone();
        let n = q.options.len();
        if let Some(o) = &mut self.other {
            match k.code {
                KeyCode::Enter if !o.trim().is_empty() => {
                    let typed = o.trim().to_owned();
                    let ans = if q.multi {
                        let mut v: Vec<String> = q.options.iter().zip(&self.checked).filter(|(_, c)| **c).map(|((l, _), _)| l.clone()).collect();
                        v.push(typed);
                        v.join(", ")
                    } else {
                        typed
                    };
                    return self.finish(ans);
                }
                KeyCode::Backspace => {
                    if o.pop().is_none() {
                        self.other = None; // empty: back to the list
                    }
                }
                KeyCode::Char(c) if !k.modifiers.contains(KeyModifiers::CONTROL) => o.push(c),
                _ => {}
            }
            return None;
        }
        let pick = |me: &mut Qs, i: usize| -> Option<Reply> {
            if i == n {
                me.other = Some(String::new());
                None
            } else if q.multi {
                me.checked[i] = !me.checked[i];
                None
            } else {
                me.finish(q.options[i].0.clone())
            }
        };
        match k.code {
            KeyCode::Up | KeyCode::Char('k') => self.cursor = self.cursor.saturating_sub(1),
            KeyCode::Down | KeyCode::Char('j') => self.cursor = (self.cursor + 1).min(self.rows() - 1),
            KeyCode::Char(' ') if q.multi && self.cursor < n => self.checked[self.cursor] = !self.checked[self.cursor],
            KeyCode::Char(c @ '1'..='9') if (c as usize - '1' as usize) < self.rows() => {
                let i = c as usize - '1' as usize;
                self.cursor = i;
                return pick(self, i);
            }
            KeyCode::Enter if q.multi && self.cursor < n => {
                // Enter confirms a multi-select once something is ticked; before that it ticks
                if self.checked.iter().any(|c| *c) {
                    let ans = q.options.iter().zip(&self.checked).filter(|(_, c)| **c).map(|((l, _), _)| l.clone()).collect::<Vec<_>>().join(", ");
                    return self.finish(ans);
                }
                self.checked[self.cursor] = true;
            }
            KeyCode::Enter => return pick(self, self.cursor),
            KeyCode::Left | KeyCode::Char('b') if self.at > 0 => {
                self.at -= 1;
                self.done.pop();
                self.cursor = 0;
                self.checked = vec![false; self.qs[self.at].options.len()];
            }
            _ => {}
        }
        None
    }

    /// The question as wrapped lines, and the line the cursor sits on.
    fn lines(&self, inner: usize, th: &Theme) -> (Vec<Line<'static>>, usize) {
        let q = &self.qs[self.at];
        let mut out: Vec<Line<'static>> = Vec::new();
        let head = if q.header.is_empty() { format!("Question {}/{}", self.at + 1, self.qs.len()) } else { format!("{} ({}/{})", q.header, self.at + 1, self.qs.len()) };
        out.push(Line::from(Span::styled(clean(&head), th.accent)));
        out.extend(wrap::wrap(&[Span::styled(clean(&q.question), th.heading)], inner, &[], &[]));
        out.push(Line::default());
        let mut cur = 0;
        for i in 0..self.rows() {
            let (label, desc) = q.options.get(i).cloned().unwrap_or(("Other (type your own answer)".to_owned(), String::new()));
            let mark = if q.multi && i < q.options.len() { if self.checked[i] { "[x]" } else { "[ ]" } } else { "   " };
            let st = if i == self.cursor { th.select } else { th.text };
            if i == self.cursor {
                cur = out.len();
            }
            out.push(Line::from(Span::styled(format!("{mark} {}. {}", i + 1, clean(&label)), st)));
            if !desc.is_empty() {
                out.extend(wrap::wrap(&[Span::styled(clean(&desc), th.muted)], inner.saturating_sub(7), &[Span::raw("       ")], &[Span::raw("       ")]));
            }
        }
        (out, cur)
    }
}

pub struct Permission {
    pub ask: Ask,
    /// Keys act only once this has passed. Any key pressed before it pushes it out again, so a
    /// burst of typing that started before the prompt appeared can never answer it.
    pub armed_at: Instant,
    pub reason: Option<String>,
    /// How far the body is scrolled, and the furthest it can go (set while drawing). Allowing is
    /// refused until the whole body has been shown, so a prompt cannot be approved unread.
    scroll: Cell<usize>,
    max_scroll: Cell<usize>,
    /// Some for an AskUserQuestion: the prompt is answered by picking, not by allow/deny.
    qs: Option<Qs>,
}

fn s<'a>(m: &'a Map<String, Value>, k: &str) -> &'a str {
    m.get(k).and_then(Value::as_str).unwrap_or("")
}

const WRITE_LINES: usize = 40;

/// What the prompt shows of the tool call. Never truncates a command; an Edit shows a -/+ diff.
pub fn body_lines(ask: &Ask) -> Vec<String> {
    let i = &ask.input;
    match ask.tool.as_str() {
        "Bash" if i.contains_key("command") => s(i, "command").split('\n').map(str::to_owned).collect(),
        "Edit" | "MultiEdit" if i.contains_key("file_path") => {
            let mut v = vec![s(i, "file_path").to_owned()];
            v.extend(s(i, "old_string").lines().map(|l| format!("-{l}")));
            v.extend(s(i, "new_string").lines().map(|l| format!("+{l}")));
            v
        }
        "Write" if i.contains_key("file_path") => {
            let mut v = vec![s(i, "file_path").to_owned()];
            let all: Vec<&str> = s(i, "content").lines().collect();
            v.extend(all.iter().take(WRITE_LINES).map(|l| (*l).to_owned()));
            if all.len() > WRITE_LINES {
                v.push(format!("... {} more lines not shown", all.len() - WRITE_LINES));
            }
            v
        }
        _ => serde_json::to_string_pretty(i).unwrap_or_default().lines().map(str::to_owned).collect(),
    }
}

impl Permission {
    pub fn new(ask: Ask) -> Self {
        let qs = if ask.tool == "AskUserQuestion" { Qs::parse(&ask.input) } else { None };
        Permission { ask, armed_at: Instant::now() + ARM, reason: None, scroll: Cell::new(0), max_scroll: Cell::new(0), qs }
    }

    pub fn armed(&self) -> bool {
        Instant::now() >= self.armed_at
    }

    /// Whether the whole body has been on screen.
    pub fn read_to_the_end(&self) -> bool {
        self.scroll.get() >= self.max_scroll.get()
    }

    /// Mouse wheel over the body. Scrolling cannot answer the prompt, so it is not held by the
    /// arming delay; reaching the end by wheel counts as reading it.
    pub fn wheel(&mut self, down: bool) {
        const STEP: usize = 3;
        let at = self.scroll.get();
        self.scroll.set(if down { (at + STEP).min(self.max_scroll.get()) } else { at.saturating_sub(STEP) });
    }

    pub fn key(&mut self, k: KeyEvent) -> Option<Reply> {
        self.key_at(k, Instant::now())
    }

    /// `key` with an explicit clock (tests drive time without sleeping).
    pub fn key_at(&mut self, k: KeyEvent, now: Instant) -> Option<Reply> {
        let deny = |reason: &str| Some(Reply { decision: Decision::Deny, reason: reason.to_owned(), answers: None });
        if k.code == KeyCode::Esc {
            return deny(""); // deny is never held back
        }
        if now < self.armed_at {
            self.armed_at = now + ARM; // somebody is typing: wait for quiet
            return None;
        }
        if let Some(qs) = &mut self.qs {
            return qs.key(k);
        }
        if let Some(r) = &mut self.reason {
            match k.code {
                KeyCode::Enter => return deny(&r.clone()),
                KeyCode::Backspace => {
                    r.pop();
                }
                KeyCode::Char(c) if !k.modifiers.contains(KeyModifiers::CONTROL) => r.push(c),
                _ => {}
            }
            return None;
        }
        let page = 8;
        match k.code {
            KeyCode::Up | KeyCode::Char('k') => self.scroll.set(self.scroll.get().saturating_sub(1)),
            KeyCode::Down | KeyCode::Char('j') => self.scroll.set((self.scroll.get() + 1).min(self.max_scroll.get())),
            KeyCode::PageUp => self.scroll.set(self.scroll.get().saturating_sub(page)),
            KeyCode::PageDown => self.scroll.set((self.scroll.get() + page).min(self.max_scroll.get())),
            KeyCode::Char('n' | '3') => self.reason = Some(String::new()),
            KeyCode::Char('y' | '1') if self.read_to_the_end() => return Some(Reply { decision: Decision::Allow, reason: String::new(), answers: None }),
            KeyCode::Char('a' | '2') if self.read_to_the_end() => return Some(Reply { decision: Decision::AllowSession, reason: String::new(), answers: None }),
            _ => {}
        }
        None
    }

    pub fn draw(&self, area: Rect, buf: &mut Buffer, th: &Theme) {
        if area.width < 12 || area.height < 5 {
            return; // too small to show anything useful; Esc still denies
        }
        let w = area.width.saturating_sub(4).clamp(10, 100).min(area.width);
        let inner = w.saturating_sub(4) as usize;
        let who = if self.ask.organ.is_empty() { "rabble".to_owned() } else { self.ask.organ.clone() };
        let head = self.ask.title.clone().unwrap_or_else(|| format!("{who} wants to use {}", self.ask.tool));
        let mut top: Vec<Line<'static>> = Vec::new();
        top.push(Line::from(vec![Span::styled(format!("{who} "), th.organ(&who)), Span::styled(format!("asks: {}", self.ask.tool), th.heading)]));
        top.extend(wrap::wrap(&[Span::styled(clean(&head), th.text)], inner, &[], &[]).into_iter().take(2));
        if let Some(d) = &self.ask.description {
            top.extend(wrap::wrap(&[Span::styled(clean(d), th.muted)], inner, &[], &[]).into_iter().take(2));
        }
        if let Some(p) = &self.ask.blocked_path {
            top.push(Line::from(Span::styled(format!("path: {}", clean(p)), th.warn)));
        }
        let mut body: Vec<Line<'static>> = Vec::new();
        let mut cursor_line = None;
        if let Some(qs) = &self.qs {
            let (l, c) = qs.lines(inner, th);
            body = l;
            cursor_line = Some(c);
        }
        for l in if self.qs.is_some() { Vec::new() } else { body_lines(&self.ask) } {
            let st = if l.starts_with('-') && self.ask.tool.contains("Edit") { th.error } else if l.starts_with('+') && self.ask.tool.contains("Edit") { th.ok } else { th.code };
            body.extend(wrap::wrap(&[Span::styled(clean(&l), st)], inner, &[], &[Span::raw("  ")]));
        }
        let controls = if let Some(qs) = &self.qs {
            if !self.armed() {
                vec![Line::from(Span::styled("read it first ...", th.muted))]
            } else if let Some(o) = &qs.other {
                vec![Line::from(vec![Span::styled("your answer> ", th.accent), Span::styled(format!("{}_", clean(o)), th.text)]), Line::from(Span::styled("Enter sends, Backspace on empty goes back, Esc declines", th.muted))]
            } else {
                let more = if qs.qs[qs.at].multi { "Space tick  Enter confirm" } else { "Enter pick" };
                vec![Line::from(Span::styled(format!("Up/Down move  1-9 jump  {more}  Left back  Esc decline"), th.muted))]
            }
        } else if let Some(r) = &self.reason {
            vec![Line::from(vec![Span::styled("reason> ", th.accent), Span::styled(format!("{}_", clean(r)), th.text)]), Line::from(Span::styled("Enter denies with this reason, Esc denies", th.muted))]
        } else if !self.armed() {
            vec![Line::from(Span::styled("read it first ...", th.muted))]
        } else if !self.read_to_the_end() {
            vec![Line::from(vec![Span::styled("Down/PageDown: read the rest before allowing   ", th.warn), Span::styled("[n] deny  [Esc] deny", th.error)])]
        } else {
            vec![Line::from(vec![Span::styled("[y] allow  ", th.ok), Span::styled("[a] allow for session  ", th.secondary), Span::styled("[n] deny  ", th.error), Span::styled("[Esc] deny", th.muted)])]
        };
        // borders 2 + top + controls fixed; the body gets the rest and scrolls, never the controls
        let fixed = 2 + top.len() + controls.len() + 1;
        let room = (area.height as usize).saturating_sub(fixed);
        let marker = body.len() > room && room > 1;
        let rows = if marker { room - 1 } else { room.min(body.len()) };
        let max = body.len().saturating_sub(rows);
        self.max_scroll.set(max);
        self.scroll.set(self.scroll.get().min(max));
        if let Some(c) = cursor_line {
            // the cursor row is always on screen
            if c < self.scroll.get() {
                self.scroll.set(c);
            } else if rows > 0 && c >= self.scroll.get() + rows {
                self.scroll.set(c + 1 - rows);
            }
        }
        let from = self.scroll.get();
        let mut shown: Vec<Line<'static>> = body.into_iter().skip(from).take(rows).collect();
        if marker {
            shown.push(Line::from(Span::styled(format!("lines {}-{} of {} (Up/Down scroll)", from + 1, from + rows, rows + max), th.muted)));
        }
        let h = (2 + top.len() + shown.len() + 1 + controls.len()).min(area.height as usize) as u16;
        let r = Rect { x: area.x + (area.width - w) / 2, y: area.y + (area.height - h) / 2, width: w, height: h };
        frame(r, buf, th, "");
        let mut y = r.y + 1;
        for l in top.iter().chain(&shown).chain(std::iter::once(&Line::default())).chain(&controls) {
            if y >= r.y + r.height - 1 {
                break;
            }
            buf.set_line(r.x + 2, y, l, r.width.saturating_sub(4));
            y += 1;
        }
    }
}

/// Box with a border and a filled background.
pub fn frame(r: Rect, buf: &mut Buffer, th: &Theme, title: &str) {
    if r.width < 2 || r.height < 2 {
        return;
    }
    Clear.render(r, buf);
    if th.modal.bg.is_some() {
        for y in r.y..r.y + r.height {
            for x in r.x..r.x + r.width {
                buf[(x, y)].set_style(th.modal);
            }
        }
    }
    let g = th.glyphs;
    let w = r.width as usize;
    let mut top = format!("{}{}{}", g.corner[0], g.rule.to_string().repeat(w.saturating_sub(2)), g.corner[1]);
    if !title.is_empty() && w > title.len() + 6 {
        top = format!("{}{} {title} {}{}", g.corner[0], g.rule, g.rule.to_string().repeat(w - title.len() - 5), g.corner[1]);
    }
    buf.set_line(r.x, r.y, &Line::from(Span::styled(top, th.tertiary)), r.width);
    buf.set_line(r.x, r.y + r.height - 1, &Line::from(Span::styled(format!("{}{}{}", g.corner[2], g.rule.to_string().repeat(w.saturating_sub(2)), g.corner[3]), th.tertiary)), r.width);
    for y in r.y + 1..r.y + r.height - 1 {
        buf.set_line(r.x, y, &Line::from(Span::styled(g.vert.to_string(), th.tertiary)), 1);
        buf.set_line(r.x + r.width - 1, y, &Line::from(Span::styled(g.vert.to_string(), th.tertiary)), 1);
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum Picked {
    None,
    Close,
    Choose(String),
}

pub struct Picker {
    pub title: String,
    pub items: Vec<(String, String)>, // (label, value)
    pub filter: Option<String>,       // Some = typing filters
    pub sel: usize,
    pub empty: String,
}

impl Picker {
    pub fn new(title: &str, items: Vec<(String, String)>, filterable: bool, empty: &str, current: Option<&str>) -> Self {
        let sel = current.and_then(|c| items.iter().position(|(_, v)| v == c)).unwrap_or(0);
        Picker { title: title.into(), items, filter: filterable.then(String::new), sel, empty: empty.into() }
    }

    pub fn shown(&self) -> Vec<&(String, String)> {
        let n = self.filter.as_deref().unwrap_or("").to_lowercase();
        self.items.iter().filter(|(l, _)| l.to_lowercase().contains(n.trim())).collect()
    }

    pub fn key(&mut self, k: KeyEvent) -> Picked {
        let n = self.shown().len();
        match k.code {
            KeyCode::Esc => Picked::Close,
            KeyCode::Up => {
                self.sel = self.sel.saturating_sub(1);
                Picked::None
            }
            KeyCode::Down => {
                self.sel = (self.sel + 1).min(n.saturating_sub(1));
                Picked::None
            }
            KeyCode::Enter => self.shown().get(self.sel).map_or(Picked::None, |(_, v)| Picked::Choose(v.clone())),
            KeyCode::Backspace => {
                if let Some(f) = &mut self.filter {
                    f.pop();
                    self.sel = 0;
                }
                Picked::None
            }
            KeyCode::Char(c) if !k.modifiers.contains(KeyModifiers::CONTROL) => {
                if let Some(f) = &mut self.filter {
                    f.push(c);
                    self.sel = 0;
                }
                Picked::None
            }
            _ => Picked::None,
        }
    }

    pub fn draw(&self, area: Rect, buf: &mut Buffer, th: &Theme) {
        if area.width < 12 || area.height < 5 {
            return;
        }
        let shown = self.shown();
        let w = area.width.saturating_sub(4).clamp(10, 90).min(area.width);
        let want = (shown.len().clamp(1, 14) + 4 + usize::from(self.filter.is_some())) as u16;
        let h = want.min(area.height);
        let rows = (h as usize).saturating_sub(4 + usize::from(self.filter.is_some())).max(1);
        let r = Rect { x: area.x + (area.width - w) / 2, y: area.y + (area.height - h) / 2, width: w, height: h };
        frame(r, buf, th, &self.title);
        let (inner, last) = (r.width as usize - 4, r.y + r.height - 1);
        let mut y = r.y + 1;
        if let Some(f) = &self.filter {
            buf.set_line(r.x + 2, y, &Line::from(vec![Span::styled("filter> ", th.accent), Span::styled(clean(f), th.text)]), inner as u16);
            y += 1;
        }
        if shown.is_empty() {
            if y < last {
                buf.set_line(r.x + 2, y, &Line::from(Span::styled(if self.items.is_empty() { self.empty.clone() } else { "No match".to_owned() }, th.muted)), inner as u16);
            }
            return;
        }
        let first = self.sel.saturating_sub(rows - 1);
        for (i, (label, _)) in shown.iter().enumerate().skip(first).take(rows) {
            if y >= last {
                break;
            }
            let st = if i == self.sel { th.select } else { th.text };
            let mut t = clean(label);
            while wrap::str_width(&t) > inner {
                t.pop();
            }
            buf.set_line(r.x + 2, y, &Line::from(Span::styled(format!("{t:<inner$}"), st)), inner as u16);
            y += 1;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::Glyphs;

    fn th() -> Theme {
        Theme::new(None, true, Glyphs::SAFE)
    }
    fn key(c: KeyCode) -> KeyEvent {
        KeyEvent::new(c, KeyModifiers::NONE)
    }
    fn ask(tool: &str, v: Value) -> Ask {
        Ask { id: "1".into(), organ: "rabble".into(), tool: tool.into(), input: v.as_object().unwrap().clone(), title: None, description: None, blocked_path: None }
    }
    fn armed(a: Ask) -> Permission {
        let mut p = Permission::new(a);
        p.armed_at = Instant::now() - Duration::from_secs(1);
        p
    }
    fn drive(p: &mut Permission, keys: &[KeyCode]) -> Option<Reply> {
        keys.iter().find_map(|k| p.key(key(*k)))
    }
    fn rep(d: Decision, r: &str) -> Option<Reply> {
        Some(Reply { decision: d, reason: r.into(), answers: None })
    }
    fn bash() -> Ask {
        ask("Bash", serde_json::json!({"command": "ls"}))
    }

    #[test]
    fn allow_keys() {
        assert_eq!(drive(&mut armed(bash()), &[KeyCode::Char('y')]), rep(Decision::Allow, ""));
        assert_eq!(drive(&mut armed(bash()), &[KeyCode::Char('1')]), rep(Decision::Allow, ""));
    }
    #[test]
    fn allow_session() {
        assert_eq!(drive(&mut armed(bash()), &[KeyCode::Char('a')]), rep(Decision::AllowSession, ""));
    }
    #[test]
    fn deny_with_reason() {
        let mut keys = vec![KeyCode::Char('n')];
        keys.extend("too risky".chars().map(KeyCode::Char));
        keys.push(KeyCode::Enter);
        assert_eq!(drive(&mut armed(bash()), &keys), rep(Decision::Deny, "too risky"));
    }
    #[test]
    fn deny_empty_reason() {
        assert_eq!(drive(&mut armed(bash()), &[KeyCode::Char('n'), KeyCode::Enter]), rep(Decision::Deny, ""));
    }
    #[test]
    fn escape_denies() {
        assert_eq!(drive(&mut armed(bash()), &[KeyCode::Esc]), rep(Decision::Deny, ""));
    }

    #[test]
    fn keys_inside_the_arming_window_are_ignored_but_escape_is_not() {
        let mut p = Permission::new(bash());
        assert_eq!(drive(&mut p, &[KeyCode::Char('a'), KeyCode::Char('y'), KeyCode::Char('n'), KeyCode::Enter]), None);
        assert_eq!(drive(&mut p, &[KeyCode::Esc]), rep(Decision::Deny, ""));
    }

    #[test]
    fn bash_command_is_not_truncated() {
        let cmd = format!("echo {}", "x".repeat(5000));
        assert_eq!(body_lines(&ask("Bash", serde_json::json!({"command": cmd.clone()}))), [cmd]);
    }

    #[test]
    fn edit_shows_a_diff() {
        let l = body_lines(&ask("Edit", serde_json::json!({"file_path": "/a.py", "old_string": "one\n", "new_string": "two\n"})));
        assert_eq!(l[0], "/a.py");
        assert!(l.contains(&"-one".to_owned()) && l.contains(&"+two".to_owned()));
    }

    fn screen(p: &Permission, w: u16, h: u16) -> Vec<String> {
        let area = Rect::new(0, 0, w, h);
        let mut b = Buffer::empty(area);
        p.draw(area, &mut b, &th());
        (0..h).map(|y| (0..w).map(|x| b[(x, y)].symbol().to_owned()).collect::<String>()).collect()
    }

    #[test]
    fn the_organ_is_named() {
        let mut a = bash();
        a.organ = "gobble".into();
        assert!(screen(&armed(a), 80, 20).join("\n").contains("gobble"));
    }

    #[test]
    fn answer_controls_and_reason_stay_on_screen_in_a_short_pane() {
        let mut a = ask("Bash", serde_json::json!({"command": "git push origin main\n".repeat(30)}));
        a.title = Some("Claude wants to run: git push".into());
        a.description = Some("Push the branch.".into());
        for rows in [40u16, 24, 16, 10, 8] {
            let p = armed(a.clone());
            assert!(screen(&p, 100, rows).join("\n").contains("[y] allow"), "controls at {rows} rows");
            let mut p = armed(a.clone());
            p.key(key(KeyCode::Char('n')));
            assert!(screen(&p, 100, rows).join("\n").contains("reason>"), "reason at {rows} rows");
        }
    }

    #[test]
    fn picker_enter_down_enter_escape_and_empty() {
        let items = vec![("newest work".to_owned(), "s-new".to_owned()), ("older".into(), "s-old".into())];
        let mut p = Picker::new("Resume", items.clone(), true, "No earlier sessions here", None);
        assert_eq!(p.key(key(KeyCode::Enter)), Picked::Choose("s-new".into()));
        let mut p = Picker::new("Resume", items.clone(), true, "", None);
        p.key(key(KeyCode::Down));
        assert_eq!(p.key(key(KeyCode::Enter)), Picked::Choose("s-old".into()));
        assert_eq!(Picker::new("Resume", items.clone(), true, "", None).key(key(KeyCode::Esc)), Picked::Close);
        let mut p = Picker::new("Resume", items, true, "", None);
        for c in "old".chars() {
            p.key(key(KeyCode::Char(c)));
        }
        assert_eq!(p.shown().len(), 1);
        let e = Picker::new("Resume", vec![], true, "No earlier sessions here", None);
        let area = Rect::new(0, 0, 60, 12);
        let mut b = Buffer::empty(area);
        e.draw(area, &mut b, &th());
        let all: String = (0..12).map(|y| (0..60).map(|x| b[(x, y)].symbol().to_owned()).collect::<String>()).collect();
        assert!(all.contains("No earlier sessions here"));
    }

    #[test]
    fn the_model_picker_starts_on_the_current_model() {
        let items = vec![("default".to_owned(), "".to_owned()), ("opus".into(), "opus".into())];
        assert_eq!(Picker::new("Model", items, false, "", Some("opus")).sel, 1);
    }

    #[test]
    fn continuous_typing_never_answers_even_past_the_arming_window() {
        // 30 keys at 60 ms: 1.8 s of typing, far past the 400 ms window, every gap shorter than it
        let mut p = Permission::new(bash());
        let t0 = Instant::now();
        for i in 0..30u32 {
            let c = ['y', 'a', '1', '2', 'n'][i as usize % 5];
            assert_eq!(p.key_at(key(KeyCode::Char(c)), t0 + Duration::from_millis(60 * i as u64)), None, "key {i}");
        }
        assert!(p.reason.is_none());
        // quiet for longer than the window, then a deliberate key works
        let quiet = t0 + Duration::from_millis(60 * 29 + 500);
        assert_eq!(p.key_at(key(KeyCode::Char('y')), quiet), rep(Decision::Allow, ""));
        // and deny stays available throughout
        let mut p = Permission::new(bash());
        assert_eq!(p.key_at(key(KeyCode::Esc), t0), rep(Decision::Deny, ""));
    }

    #[test]
    fn enter_is_not_an_allow_key() {
        assert_eq!(drive(&mut armed(bash()), &[KeyCode::Enter]), None);
    }

    #[test]
    fn modals_do_not_panic_in_tiny_areas() {
        let items = vec![("a".to_owned(), "a".to_owned())];
        for w in [0u16, 1, 4, 6, 9, 10, 11, 12, 13] {
            for h in [0u16, 1, 3, 5, 6, 9] {
                let area = Rect::new(0, 0, w, h);
                let mut b = Buffer::empty(area);
                armed(bash()).draw(area, &mut b, &th());
                let mut p = armed(bash());
                p.key(key(KeyCode::Char('n')));
                p.draw(area, &mut b, &th());
                Picker::new("T", items.clone(), true, "e", None).draw(area, &mut b, &th());
                Picker::new("T", vec![], true, "e", None).draw(area, &mut b, &th());
                frame(area, &mut b, &th(), "title");
            }
        }
    }

    #[test]
    fn a_clipped_body_cannot_be_allowed_until_it_has_been_read() {
        let a = ask("Bash", serde_json::json!({"command": (0..60).map(|i| format!("line {i}")).collect::<Vec<_>>().join("\n")}));
        let mut p = armed(a);
        let area = Rect::new(0, 0, 100, 16);
        let mut b = Buffer::empty(area);
        p.draw(area, &mut b, &th());
        assert_eq!(drive(&mut p, &[KeyCode::Char('y'), KeyCode::Char('a')]), None, "clipped: allow keys are dead");
        assert_eq!(p.key(key(KeyCode::Char('n'))), None);
        p.reason = None;
        for _ in 0..80 {
            p.key(key(KeyCode::Down));
        }
        p.draw(area, &mut b, &th());
        assert!(p.read_to_the_end());
        let text: String = (0..16).map(|y| (0..100).map(|x| b[(x, y)].symbol().to_owned()).collect::<String>()).collect();
        assert!(text.contains("line 59") && text.contains("[y] allow"), "{text}");
        assert_eq!(drive(&mut p, &[KeyCode::Char('y')]), rep(Decision::Allow, ""));
    }

    #[test]
    fn the_wheel_scrolls_the_body_and_counts_as_reading_it() {
        let a = ask("Bash", serde_json::json!({"command": (0..60).map(|i| format!("line {i}")).collect::<Vec<_>>().join("\n")}));
        let mut p = armed(a);
        let area = Rect::new(0, 0, 100, 16);
        let mut b = Buffer::empty(area);
        p.draw(area, &mut b, &th());
        p.wheel(false);
        assert_eq!(p.scroll.get(), 0, "cannot scroll above the top");
        p.wheel(true);
        assert_eq!(p.scroll.get(), 3);
        for _ in 0..40 {
            p.wheel(true);
        }
        p.draw(area, &mut b, &th());
        assert!(p.read_to_the_end());
        assert_eq!(drive(&mut p, &[KeyCode::Char('y')]), rep(Decision::Allow, ""));
    }

    #[test]
    fn write_shows_how_many_lines_were_cut() {
        let l = body_lines(&ask("Write", serde_json::json!({"file_path": "/f", "content": "x\n".repeat(100)})));
        assert!(l.last().unwrap().contains("60 more lines not shown"), "{l:?}");
    }

    #[test]
    fn escape_sequences_in_a_prompt_never_reach_a_cell() {
        let mut a = ask("Bash", serde_json::json!({"command": "echo \u{1b}]52;c;QQ==\u{7}hi \u{1b}[2J"}));
        a.title = Some("t\u{1b}[31m".into());
        let area = Rect::new(0, 0, 90, 20);
        let mut b = Buffer::empty(area);
        armed(a).draw(area, &mut b, &th());
        assert!(b.content().iter().all(|c| !c.symbol().chars().any(char::is_control)));
    }

    fn asking() -> Ask {
        ask("AskUserQuestion", serde_json::json!({"questions": [
            {"question": "Which db?", "header": "DB", "multiSelect": false, "options": [{"label": "sqlite", "description": "local"}, {"label": "postgres", "description": "server"}]},
            {"question": "Which extras?", "header": "Extras", "multiSelect": true, "options": [{"label": "tests", "description": ""}, {"label": "docs", "description": ""}]}
        ]}))
    }

    #[test]
    fn a_question_is_answered_by_picking_and_the_answers_go_back() {
        let mut p = armed(asking());
        assert_eq!(drive(&mut p, &[KeyCode::Char('y')]), None, "y does not answer a question");
        let keys = [KeyCode::Down, KeyCode::Enter, KeyCode::Char(' '), KeyCode::Down, KeyCode::Char(' '), KeyCode::Enter];
        let r = drive(&mut p, &keys).expect("answered");
        assert_eq!(r.decision, Decision::Allow);
        let a = r.answers.unwrap();
        assert_eq!(a["Which db?"], "postgres");
        assert_eq!(a["Which extras?"], "tests, docs");
    }

    #[test]
    fn a_number_picks_and_other_takes_typed_text_and_esc_declines() {
        let mut p = armed(asking());
        p.key(key(KeyCode::Char('1')));
        let mut keys = vec![KeyCode::Char('3')];
        keys.extend("ok".chars().map(KeyCode::Char));
        keys.push(KeyCode::Enter);
        let a = drive(&mut p, &keys).unwrap().answers.unwrap();
        assert_eq!((a["Which db?"].as_str(), a["Which extras?"].as_str()), ("sqlite", "ok"));
        let mut p = armed(asking());
        assert_eq!(drive(&mut p, &[KeyCode::Esc]), rep(Decision::Deny, ""));
    }

    #[test]
    fn a_question_draws_its_options_and_keeps_the_cursor_on_screen() {
        let mut p = armed(asking());
        let text = screen(&p, 80, 20).join("\n");
        assert!(text.contains("Which db?") && text.contains("1. sqlite") && text.contains("Other"), "{text}");
        p.key(key(KeyCode::Down));
        p.key(key(KeyCode::Down));
        for rows in [20u16, 9, 8] {
            assert!(screen(&p, 80, rows).join("\n").contains("3. Other"), "cursor visible at {rows} rows");
        }
    }
}
