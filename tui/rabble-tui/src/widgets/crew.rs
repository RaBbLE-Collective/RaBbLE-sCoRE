//! The crew on screen: a side panel (who is in it, who is working, how they hand work around) and a
//! viewer that reads one member's transcript. Both only read `crate::crew::Crew`.

use std::cell::Cell;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{buffer::Buffer, layout::Rect, style::Style, text::{Line, Span}, widgets::Clear, widgets::Widget};

use crate::{
    crew::{display, Crew, Flow, Kind, Member, State, LEAD},
    theme::Theme,
    wrap::{self, clean},
};

pub const PANEL_WIDTH: u16 = 34;
/// The panel only opens beside a transcript at least this wide.
pub const PANEL_MIN_AREA: u16 = 96;

fn fit(s: &str, w: usize) -> String {
    let mut t = clean(s);
    while wrap::str_width(&t) > w {
        t.pop();
    }
    t
}

fn who(th: &Theme, name: &str) -> Style {
    if name == "you" { th.user } else { th.organ(name) }
}

fn status(th: &Theme, m: &Member, lead_busy: bool) -> (String, Style) {
    let secs = m.secs().map(|s| format!(" {s}s")).unwrap_or_default();
    match (m.state, m.name == LEAD) {
        (_, true) => if lead_busy { ("working".into(), th.accent) } else { ("idle".into(), th.muted) },
        (State::Idle, _) => ("idle".into(), th.muted),
        (State::Active, _) => (format!("working{secs}"), th.accent),
        (State::Waiting, _) => ("needs you".into(), th.warn),
        (State::Done, _) => (format!("done{secs}"), th.ok),
        (State::Failed, _) => (format!("failed{secs}"), th.error),
        (State::Killed, _) => (format!("stopped{secs}"), th.error),
    }
}

fn tree(th: &Theme, last: bool) -> (&'static str, &'static str) {
    if th.glyphs.rule == '-' {
        if last { ("`- ", "   ") } else { ("|- ", "|  ") }
    } else if last {
        ("└─ ", "   ")
    } else {
        ("├─ ", "│  ")
    }
}

/// Roster as a tree under the lead, then the latest hand-offs. `area` is the whole panel.
pub fn panel(crew: &Crew, area: Rect, buf: &mut Buffer, th: &Theme, lead_busy: bool) {
    if area.width < 12 || area.height < 3 {
        return;
    }
    Clear.render(area, buf);
    for y in area.y..area.y + area.height {
        buf.set_line(area.x, y, &Line::from(Span::styled(th.glyphs.vert.to_string(), th.border)), 1);
    }
    let x = area.x + 2;
    let w = (area.width - 2) as usize;
    let mut y = area.y;
    let put = |buf: &mut Buffer, line: Line<'static>, y: &mut u16| {
        if *y < area.y + area.height {
            buf.set_line(x, *y, &line, w as u16);
            *y += 1;
        }
    };
    let head = if crew.organs().next().is_none() {
        "CREW".to_owned()
    } else {
        format!("CREW  {} working{}", crew.active(), if crew.waiting() > 0 { format!(" · {} wait", crew.waiting()) } else { String::new() })
    };
    put(buf, Line::from(Span::styled(fit(&head, w), th.heading)), &mut y);
    if let Some(lead) = crew.get(LEAD) {
        let (st, sty) = status(th, lead, lead_busy);
        put(buf, Line::from(vec![Span::styled(format!("{} ", th.glyphs.bullet), sty), Span::styled(display(LEAD).to_owned(), th.text), Span::styled(format!("  {st}"), sty)]), &mut y);
    }
    let organs: Vec<&Member> = crew.organs().collect();
    if organs.is_empty() {
        put(buf, Line::from(Span::styled(fit("no organs yet", w), th.muted)), &mut y);
    }
    for (i, m) in organs.iter().enumerate() {
        let (branch, cont) = tree(th, i + 1 == organs.len());
        let (st, sty) = status(th, m, false);
        put(buf, Line::from(vec![Span::styled(branch, th.border), Span::styled(format!("{} ", th.glyphs.bullet), sty), Span::styled(fit(display(&m.name), 10), who(th, &m.name)), Span::styled(format!("  {}", fit(&st, w.saturating_sub(18))), sty)]), &mut y);
        let doing = if m.state.live() && !m.last_tool.is_empty() { m.last_tool.clone() } else { m.brief.clone() };
        if !doing.is_empty() {
            put(buf, Line::from(vec![Span::styled(cont, th.border), Span::styled(fit(&doing, w.saturating_sub(5)), th.muted)]), &mut y);
        }
    }
    y += 1;
    if y + 2 >= area.y + area.height || crew.hops.is_empty() {
        return;
    }
    put(buf, Line::from(Span::styled("FLOW", th.heading)), &mut y);
    let room = (area.y + area.height).saturating_sub(y) as usize;
    let skip = crew.hops.len().saturating_sub(room);
    for h in crew.hops.iter().skip(skip) {
        let (arrow, sty) = match h.kind {
            Flow::Spawn => (th.glyphs.arrow, th.secondary),
            Flow::Return => (th.glyphs.arrow, th.ok),
            Flow::Ask => (th.glyphs.arrow, th.warn),
            Flow::Answer => (th.glyphs.arrow, th.tertiary),
        };
        let route = format!("{} {arrow} {}", h.from, h.to);
        let left = w.saturating_sub(wrap::str_width(&route) + 2);
        put(buf, Line::from(vec![Span::styled(h.from.clone(), who(th, &h.from)), Span::styled(format!(" {arrow} "), sty), Span::styled(h.to.clone(), who(th, &h.to)), Span::styled(format!("  {}", fit(&h.text, left)), th.muted)]), &mut y);
    }
}

#[derive(Debug, PartialEq)]
pub enum ViewAction {
    None,
    Close,
    /// Stop this task id (the selected organ's running task).
    Stop(String),
}

/// Reads one member's transcript. `back` counts lines up from the newest; 0 follows.
pub struct CrewView {
    pub sel: usize,
    back: usize,
    max_back: Cell<usize>,
}

impl CrewView {
    pub fn new(sel: usize) -> Self {
        CrewView { sel, back: 0, max_back: Cell::new(0) }
    }

    pub fn wheel(&mut self, down: bool) {
        self.back = if down { self.back.saturating_sub(3) } else { (self.back + 3).min(self.max_back.get()) };
    }

    pub fn key(&mut self, k: KeyEvent, crew: &Crew) -> ViewAction {
        let n = crew.members.len().max(1);
        let page = 10;
        let go = |me: &mut CrewView, to: usize| {
            me.sel = to;
            me.back = 0;
        };
        match k.code {
            KeyCode::Esc | KeyCode::Char('q') => return ViewAction::Close,
            KeyCode::Char('g') if k.modifiers.contains(KeyModifiers::CONTROL) => return ViewAction::Close,
            KeyCode::Tab | KeyCode::Right | KeyCode::Char('l') => go(self, (self.sel + 1) % n),
            KeyCode::BackTab | KeyCode::Left | KeyCode::Char('h') => go(self, (self.sel + n - 1) % n),
            KeyCode::Up | KeyCode::Char('k') => self.back = (self.back + 1).min(self.max_back.get()),
            KeyCode::Down | KeyCode::Char('j') => self.back = self.back.saturating_sub(1),
            KeyCode::PageUp => self.back = (self.back + page).min(self.max_back.get()),
            KeyCode::PageDown => self.back = self.back.saturating_sub(page),
            KeyCode::Home => self.back = self.max_back.get(),
            KeyCode::End => self.back = 0,
            KeyCode::Char('x') => {
                if let Some(m) = crew.members.get(self.sel).filter(|m| m.state.live()) {
                    if let Some(id) = &m.task_id {
                        return ViewAction::Stop(id.clone());
                    }
                }
            }
            _ => {}
        }
        ViewAction::None
    }

    pub fn draw(&self, crew: &Crew, area: Rect, buf: &mut Buffer, th: &Theme, lead_busy: bool) {
        if area.width < 24 || area.height < 4 {
            return;
        }
        Clear.render(area, buf);
        let sel = self.sel.min(crew.members.len().saturating_sub(1));
        let side = 22.min(area.width / 3);
        let (mut y, bottom) = (area.y, area.y + area.height);
        for (i, m) in crew.members.iter().enumerate() {
            if y + 1 >= bottom {
                break;
            }
            let (st, sty) = status(th, m, lead_busy);
            let name = if i == sel { th.select } else { who(th, &m.name) };
            buf.set_line(area.x, y, &Line::from(vec![Span::styled(format!("{} ", th.glyphs.bullet), sty), Span::styled(fit(display(&m.name), side as usize - 3), name)]), side);
            buf.set_line(area.x + 2, y + 1, &Line::from(Span::styled(fit(&st, side as usize - 3), th.muted)), side - 2);
            y += 2;
        }
        if y < bottom {
            buf.set_line(area.x, bottom - 1, &Line::from(Span::styled(fit("Tab  x:stop  q:close", side as usize - 1), th.muted)), side);
        }
        for yy in area.y..bottom {
            buf.set_line(area.x + side, yy, &Line::from(Span::styled(th.glyphs.vert.to_string(), th.border)), 1);
        }
        let (x, w) = (area.x + side + 2, (area.width - side - 2) as usize);
        let Some(m) = crew.members.get(sel) else { return };
        let (st, sty) = status(th, m, lead_busy);
        let mut head = vec![Span::styled(display(&m.name).to_owned(), who(th, &m.name)), Span::styled(format!("  {st}"), sty)];
        if m.tools > 0 || m.tokens > 0 {
            head.push(Span::styled(format!("  {} tools · {} tok", m.tools, m.tokens), th.muted));
        }
        buf.set_line(x, area.y, &Line::from(head), w as u16);
        if !m.brief.is_empty() {
            buf.set_line(x, area.y + 1, &Line::from(Span::styled(fit(&m.brief, w), th.muted)), w as u16);
        }
        let mut lines: Vec<Line<'static>> = Vec::new();
        for e in &m.log {
            match e.kind {
                Kind::Say => lines.extend(wrap::wrap(&[Span::styled(clean(&e.text), th.text)], w, &[], &[])),
                Kind::Tool => lines.extend(wrap::wrap(&[Span::styled(clean(&e.text), th.tertiary)], w, &[Span::styled(format!("{} ", th.glyphs.prompt), th.tertiary)], &[Span::raw("  ")])),
                Kind::Result { ok } => lines.extend(wrap::wrap(&[Span::styled(clean(&e.text), if ok { th.muted } else { th.error })], w, &[Span::raw("  ")], &[Span::raw("  ")]).into_iter().take(3)),
                Kind::Note => lines.extend(wrap::wrap(&[Span::styled(clean(&e.text), th.secondary)], w, &[Span::styled(format!("{} ", th.glyphs.bullet), th.secondary)], &[Span::raw("  ")])),
            }
        }
        if lines.is_empty() {
            lines.push(Line::from(Span::styled(if m.name == LEAD { "the lead's own words are in the main transcript" } else { "nothing from this organ yet" }, th.muted)));
        }
        let top = area.y + 3;
        let rows = bottom.saturating_sub(top) as usize;
        let max_back = lines.len().saturating_sub(rows);
        self.max_back.set(max_back);
        let back = self.back.min(max_back);
        let end = lines.len() - back;
        let from = end.saturating_sub(rows);
        for (i, l) in lines[from..end].iter().enumerate() {
            buf.set_line(x, top + i as u16, l, w as u16);
        }
        if back > 0 {
            buf.set_line(x, bottom - 1, &Line::from(Span::styled(format!("{back} lines below (End follows)"), th.warn)), w as u16);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::Glyphs;
    use rabble_proto::OrganStatus;

    fn th() -> Theme {
        Theme::new(None, true, Glyphs::SAFE)
    }
    fn key(c: KeyCode) -> KeyEvent {
        KeyEvent::new(c, KeyModifiers::NONE)
    }
    fn text(b: &Buffer, a: Rect) -> String {
        (a.y..a.y + a.height).map(|y| (a.x..a.x + a.width).map(|x| b[(x, y)].symbol().to_owned()).collect::<String>()).collect::<Vec<_>>().join("\n")
    }
    fn crew() -> Crew {
        let mut c = Crew::new();
        c.started("gobble", Some("k1".into()), "map the repo");
        c.started("cobble", Some("k2".into()), "build it");
        c.tool_call("gobble", "t1", "Grep", serde_json::json!({"pattern": "fn main"}).as_object().unwrap());
        c.ask("cobble", "a1", "Edit");
        c.started("nibble", Some("k3".into()), "review");
        c.done("nibble", &OrganStatus::Completed, "clean");
        c
    }

    #[test]
    fn the_panel_shows_who_is_in_the_crew_what_they_do_and_the_flow() {
        let a = Rect::new(0, 0, PANEL_WIDTH, 24);
        let mut b = Buffer::empty(a);
        panel(&crew(), a, &mut b, &th(), true);
        let t = text(&b, a);
        for want in ["CREW  2 working · 1 wait", "rabble", "gobble", "Grep fn main", "needs you", "done", "FLOW", "rabble -> gobble", "cobble -> you"] {
            assert!(t.contains(want), "missing {want:?} in\n{t}");
        }
    }

    #[test]
    fn an_empty_crew_and_tiny_areas_do_not_panic() {
        for (w, h) in [(0u16, 0u16), (3, 2), (11, 5), (12, 3), (34, 4), (34, 40)] {
            let a = Rect::new(0, 0, w, h);
            let mut b = Buffer::empty(a);
            panel(&Crew::new(), a, &mut b, &th(), false);
            panel(&crew(), a, &mut b, &th(), true);
            CrewView::new(1).draw(&crew(), a, &mut b, &th(), true);
        }
    }

    #[test]
    fn the_viewer_reads_the_selected_member_and_scrolls() {
        let mut c = crew();
        for i in 0..60 {
            c.note("gobble", &format!("step {i}"));
        }
        let mut v = CrewView::new(1);
        let a = Rect::new(0, 0, 80, 14);
        let mut b = Buffer::empty(a);
        v.draw(&c, a, &mut b, &th(), false);
        let t = text(&b, a);
        assert!(t.contains("step 59") && t.contains("Grep fn main") == false, "follows the newest\n{t}");
        v.key(key(KeyCode::PageUp), &c);
        v.draw(&c, a, &mut b, &th(), false);
        assert!(text(&b, a).contains("lines below"));
        v.key(key(KeyCode::End), &c);
        assert_eq!(v.back, 0);
        v.key(key(KeyCode::Tab), &c);
        assert_eq!(v.sel, 2);
    }

    #[test]
    fn x_stops_only_a_running_organ_and_q_closes() {
        let c = crew();
        let mut v = CrewView::new(1); // gobble, running
        assert_eq!(v.key(key(KeyCode::Char('x')), &c), ViewAction::Stop("k1".into()));
        let mut v = CrewView::new(3); // nibble, done
        assert_eq!(v.key(key(KeyCode::Char('x')), &c), ViewAction::None);
        let mut v = CrewView::new(0); // the lead has no task
        assert_eq!(v.key(key(KeyCode::Char('x')), &c), ViewAction::None);
        assert_eq!(v.key(key(KeyCode::Esc), &c), ViewAction::Close);
    }
}
