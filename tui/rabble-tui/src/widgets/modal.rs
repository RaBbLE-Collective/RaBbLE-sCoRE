//! Modals: the permission prompt and the pickers (model, session). They own their keys and
//! return a result; the app owns what the result means. The permission prompt arms 0.4 s after it
//! opens so a keystroke meant for the composer cannot answer an ask nobody has read; Esc (deny)
//! is never held back.

use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use rabble_proto::Decision;
use ratatui::{buffer::Buffer, layout::Rect, text::{Line, Span}, widgets::Clear, widgets::Widget};
use serde_json::{Map, Value};

use crate::{theme::Theme, wrap};

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
}

pub struct Permission {
    pub ask: Ask,
    pub armed_at: Instant,
    pub reason: Option<String>,
}

fn s<'a>(m: &'a Map<String, Value>, k: &str) -> &'a str {
    m.get(k).and_then(Value::as_str).unwrap_or("")
}

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
            v.extend(s(i, "content").lines().take(40).map(str::to_owned));
            v
        }
        _ => serde_json::to_string_pretty(i).unwrap_or_default().lines().map(str::to_owned).collect(),
    }
}

impl Permission {
    pub fn new(ask: Ask) -> Self {
        Permission { ask, armed_at: Instant::now() + ARM, reason: None }
    }

    pub fn armed(&self) -> bool {
        Instant::now() >= self.armed_at
    }

    pub fn key(&mut self, k: KeyEvent) -> Option<Reply> {
        let deny = |reason: &str| Some(Reply { decision: Decision::Deny, reason: reason.to_owned() });
        if k.code == KeyCode::Esc {
            return deny("");
        }
        if !self.armed() {
            return None;
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
        match k.code {
            KeyCode::Char('y' | '1') | KeyCode::Enter => Some(Reply { decision: Decision::Allow, reason: String::new() }),
            KeyCode::Char('a' | '2') => Some(Reply { decision: Decision::AllowSession, reason: String::new() }),
            KeyCode::Char('n' | '3') => {
                self.reason = Some(String::new());
                None
            }
            _ => None,
        }
    }

    pub fn draw(&self, area: Rect, buf: &mut Buffer, th: &Theme) {
        let w = area.width.saturating_sub(4).clamp(10, 100);
        let inner = w.saturating_sub(4) as usize;
        let who = if self.ask.organ.is_empty() { "rabble".to_owned() } else { self.ask.organ.clone() };
        let head = self.ask.title.clone().unwrap_or_else(|| format!("{who} wants to use {}", self.ask.tool));
        let mut top: Vec<Line<'static>> = Vec::new();
        top.push(Line::from(vec![Span::styled(format!("{who} "), th.organ(&who)), Span::styled(format!("asks: {}", self.ask.tool), th.heading)]));
        top.extend(wrap::wrap(&[Span::styled(head, th.text)], inner, &[], &[]).into_iter().take(2));
        if let Some(d) = &self.ask.description {
            top.extend(wrap::wrap(&[Span::styled(d.clone(), th.muted)], inner, &[], &[]).into_iter().take(2));
        }
        if let Some(p) = &self.ask.blocked_path {
            top.push(Line::from(Span::styled(format!("path: {p}"), th.warn)));
        }
        let mut body: Vec<Line<'static>> = Vec::new();
        for l in body_lines(&self.ask) {
            let st = if l.starts_with('-') && self.ask.tool.contains("Edit") { th.error } else if l.starts_with('+') && self.ask.tool.contains("Edit") { th.ok } else { th.code };
            body.extend(wrap::wrap(&[Span::styled(l, st)], inner, &[], &[Span::raw("  ")]));
        }
        let controls = if let Some(r) = &self.reason {
            vec![Line::from(vec![Span::styled("reason> ", th.accent), Span::styled(format!("{r}{}", "_"), th.text)]), Line::from(Span::styled("Enter denies with this reason, Esc denies", th.muted))]
        } else if self.armed() {
            vec![Line::from(vec![Span::styled("[y] allow  ", th.ok), Span::styled("[a] allow for session  ", th.secondary), Span::styled("[n] deny  ", th.error), Span::styled("[Esc] deny", th.muted)])]
        } else {
            vec![Line::from(Span::styled("read it first ...", th.muted))]
        };
        // borders 2 + top + controls fixed; the body gets the rest and is clipped, never the controls
        let fixed = 2 + top.len() + controls.len() + 1;
        let room = (area.height as usize).saturating_sub(fixed);
        let shown = body.len().min(room);
        if shown < body.len() && shown > 0 {
            let more = body.len() - shown + 1;
            body.truncate(shown - 1);
            body.push(Line::from(Span::styled(format!("{} {more} more lines", th.glyphs.ellipsis), th.muted)));
        } else {
            body.truncate(shown);
        }
        let h = (2 + top.len() + body.len() + 1 + controls.len()).min(area.height as usize) as u16;
        let r = Rect { x: area.x + (area.width - w) / 2, y: area.y + (area.height - h) / 2, width: w, height: h };
        frame(r, buf, th, "");
        let mut y = r.y + 1;
        for l in top.iter().chain(&body).chain(std::iter::once(&Line::default())).chain(&controls) {
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
        let shown = self.shown();
        let w = area.width.saturating_sub(4).clamp(10, 90);
        let rows = shown.len().clamp(1, 14);
        let h = ((rows + 4 + usize::from(self.filter.is_some())) as u16).min(area.height);
        let r = Rect { x: area.x + (area.width - w) / 2, y: area.y + (area.height - h) / 2, width: w, height: h };
        frame(r, buf, th, &self.title);
        let mut y = r.y + 1;
        if let Some(f) = &self.filter {
            buf.set_line(r.x + 2, y, &Line::from(vec![Span::styled("filter> ", th.accent), Span::styled(f.clone(), th.text)]), r.width - 4);
            y += 1;
        }
        if shown.is_empty() {
            buf.set_line(r.x + 2, y, &Line::from(Span::styled(if self.items.is_empty() { self.empty.clone() } else { "No match".to_owned() }, th.muted)), r.width - 4);
            return;
        }
        let first = self.sel.saturating_sub(rows - 1);
        for (i, (label, _)) in shown.iter().enumerate().skip(first).take(rows) {
            let st = if i == self.sel { th.select } else { th.text };
            let mut t = label.clone();
            while wrap::str_width(&t) > r.width as usize - 4 {
                t.pop();
            }
            buf.set_line(r.x + 2, y, &Line::from(Span::styled(format!("{t:<w$}", w = r.width as usize - 4), st)), r.width - 4);
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
        Some(Reply { decision: d, reason: r.into() })
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
}
