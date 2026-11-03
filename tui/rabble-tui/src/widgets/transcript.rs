//! The conversation: user lines, replies (Markdown), tool cards, notices. Entries cache their
//! wrapped lines per width, so a frame only re-renders what changed; painting a frame copies the
//! visible window (a binary search over entry starts), so wheel cost does not grow with history.
//!
//! Scroll: 1:1 (a notch moves `WHEEL_LINES` at once, no easing). Stuck to the bottom while the
//! reader is within `SLACK` lines of it; a reader who scrolled further up is left alone. Blur
//! remembers the pin, refocus restores it and ignores a jump to the top for `REFOCUS_GUARD`
//! (terminals have been seen to deliver stray scroll-to-top around a focus change).

use std::time::{Duration, Instant};

use ratatui::{buffer::Buffer, layout::Rect, style::{Modifier, Style}, text::{Line, Span}, widgets::Widget};

use crate::widgets::select::{self, Pos, Selection};
use crate::{markdown, theme::Theme, wrap::{self, clean, str_width}};

pub const WHEEL_LINES: usize = 3;
pub const SLACK: usize = 3; // follow while the distance from the bottom is < SLACK lines
pub const REFOCUS_GUARD: Duration = Duration::from_millis(400);
/// The full face fills the pane's height, never less than this (Python `FaceWidget.-full`: `min-height: 2`).
pub const FACE_MIN_ROWS: usize = 2;
const TITLE_MAX: usize = 100;
const RESULT_MAX: usize = 60;

#[derive(Debug, Clone, PartialEq)]
pub enum Item {
    User(String),
    Reply { organ: String, text: String, streaming: bool },
    Tool { id: String, organ: String, name: String, line: String, input: String, result: Option<(bool, String)> },
    Notice { text: String, error: bool },
    Thinking,
    /// `/face`: room for the full face, which the app paints over these rows. Dropped on the next submit.
    Face,
}

struct Entry {
    item: Item,
    lines: Vec<Line<'static>>,
    start: usize,
    dirty: bool,
}

pub struct Transcript {
    entries: Vec<Entry>,
    width: u16,
    total: usize,
    max: usize,
    stale: bool,
    pub offset: usize,
    pub follow: bool,
    pub expanded: bool,
    blurred: Option<bool>,
    guard_until: Option<(Instant, bool)>,
    pub view_h: u16,
    face_rows: usize,
    pub sel: Selection,
    /// Where the entry holding the selection starts; if it moves, the selection no longer points at its text.
    /// (entry index, its start line), taken at the press so an upward drag into an earlier entry keeps the anchor.
    sel_start: Option<(usize, usize)>,
}

pub fn one_line(data: &serde_json::Map<String, serde_json::Value>) -> String {
    for k in ["command", "file_path", "path", "pattern", "description", "url", "query"] {
        if let Some(v) = data.get(k) {
            let s = match v {
                serde_json::Value::String(s) => s.clone(),
                other => other.to_string(),
            };
            if !s.is_empty() {
                return s.split_whitespace().collect::<Vec<_>>().join(" ");
            }
        }
    }
    if data.is_empty() {
        return String::new();
    }
    serde_json::Value::Object(data.clone()).to_string().split_whitespace().collect::<Vec<_>>().join(" ")
}

fn clip(s: &str, n: usize, ell: &str) -> String {
    if s.chars().count() <= n { s.to_owned() } else { format!("{}{ell}", s.chars().take(n.saturating_sub(1)).collect::<String>()) }
}

/// Cut to a display width (single line).
fn fit(s: &str, w: usize, ell: &str) -> String {
    if str_width(s) <= w {
        return s.to_owned();
    }
    let mut out = String::new();
    let mut used = 0;
    for c in s.chars() {
        let cw = wrap::cw(c);
        if used + cw + str_width(ell) > w {
            break;
        }
        out.push(c);
        used += cw;
    }
    out + ell
}

impl Default for Transcript {
    fn default() -> Self {
        Self::new()
    }
}

impl Transcript {
    pub fn new() -> Self {
        Transcript { entries: Vec::new(), width: 0, total: 0, max: 0, stale: true, offset: 0, follow: true, expanded: false, blurred: None, guard_until: None, view_h: 0, face_rows: FACE_MIN_ROWS, sel: Selection::default(), sel_start: None }
    }

    fn push(&mut self, item: Item) {
        self.entries.push(Entry { item, lines: Vec::new(), start: 0, dirty: true });
        self.stale = true;
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
    pub fn items(&self) -> impl Iterator<Item = &Item> {
        self.entries.iter().map(|e| &e.item)
    }
    pub fn total_lines(&self) -> usize {
        self.total
    }
    pub fn max_scroll(&self) -> usize {
        self.max
    }

    // the full face ------------------------------------------------------------------------
    /// `/face`: one room for the full face at the end of the conversation (replaces an earlier one).
    pub fn push_face(&mut self) {
        self.drop_face();
        self.push(Item::Face);
    }

    pub fn drop_face(&mut self) {
        let n = self.entries.len();
        self.entries.retain(|e| e.item != Item::Face);
        if self.entries.len() != n {
            self.stale = true;
            self.sel.clear();
        }
    }

    pub fn has_face(&self) -> bool {
        self.entries.iter().any(|e| e.item == Item::Face)
    }

    pub fn face_rows(&self) -> usize {
        self.face_rows
    }

    /// Where the face is on screen inside `area` (the transcript pane): its visible part and how many of its
    /// top rows are scrolled out of view. None when it is not showing.
    pub fn face_rect(&self, area: Rect) -> Option<(Rect, u16)> {
        let e = self.entries.iter().find(|e| e.item == Item::Face)?;
        let top = self.offset.min(self.max);
        let (lo, hi) = (e.start.max(top), (e.start + e.lines.len()).min(top + area.height as usize));
        if lo >= hi {
            return None;
        }
        Some((Rect { x: area.x, y: area.y + (lo - top) as u16, width: area.width, height: (hi - lo) as u16 }, (lo - e.start) as u16))
    }

    /// The newest non-blank line: (row in the view, cells of text), clamped into the view. Where text is appearing.
    pub fn newest_line(&self) -> Option<(u16, usize)> {
        if self.stale || self.total == 0 || self.view_h == 0 {
            return None;
        }
        let i = (0..self.total).rev().take(4).find(|&i| self.line_at(i).is_some_and(|l| select::width(l) > 0))?;
        let top = self.offset.min(self.max);
        let row = i.saturating_sub(top).min(self.view_h as usize - 1) as u16;
        Some((row, self.line_at(i).map_or(0, select::width)))
    }

    // content ------------------------------------------------------------------------------
    pub fn user(&mut self, text: &str) {
        self.drop_transient();
        self.end_stream();
        self.push(Item::User(clean(text)));
        self.follow = true; // sending jumps to the bottom
    }

    pub fn end_stream(&mut self) {
        if let Some(Entry { item: Item::Reply { streaming, .. }, dirty, .. }) = self.entries.last_mut() {
            if *streaming {
                *streaming = false;
                *dirty = true;
                self.stale = true;
            }
        }
    }

    pub fn delta(&mut self, organ: &str, text: &str) {
        let (organ, text) = (clean(organ), clean(text));
        let (organ, text) = (organ.as_str(), text.as_str());
        self.drop_transient();
        if let Some(Entry { item: Item::Reply { organ: o, text: t, streaming: true }, dirty, .. }) = self.entries.last_mut() {
            if o == organ {
                t.push_str(text);
                *dirty = true;
                self.stale = true;
                return;
            }
        }
        self.end_stream();
        self.push(Item::Reply { organ: organ.into(), text: text.into(), streaming: true });
    }

    /// The finished block replaces what streamed for it (text is drawn once).
    pub fn block(&mut self, organ: &str, text: &str) {
        let (organ, text) = (clean(organ), clean(text));
        let (organ, text) = (organ.as_str(), text.as_str());
        self.drop_transient();
        if let Some(Entry { item: Item::Reply { organ: o, text: t, streaming }, dirty, .. }) = self.entries.last_mut() {
            if o == organ && *streaming {
                *t = text.to_owned();
                *streaming = false;
                *dirty = true;
                self.stale = true;
                return;
            }
        }
        self.push(Item::Reply { organ: organ.into(), text: text.into(), streaming: false });
    }

    pub fn thinking(&mut self) {
        if !self.entries.last().is_some_and(|e| e.item == Item::Thinking) {
            self.push(Item::Thinking);
        }
    }

    /// Remove the "thinking" placeholder (anything transient).
    pub fn drop_transient(&mut self) {
        if self.entries.last().is_some_and(|e| e.item == Item::Thinking) {
            self.entries.pop();
            self.stale = true;
        }
    }

    pub fn tool_call(&mut self, organ: &str, id: &str, name: &str, input: &serde_json::Map<String, serde_json::Value>) {
        self.drop_transient();
        self.end_stream();
        let pretty = clean(&serde_json::to_string_pretty(input).unwrap_or_default());
        self.push(Item::Tool { id: id.into(), organ: clean(organ), name: clean(name), line: clip(&clean(&one_line(input)), TITLE_MAX, "…"), input: pretty, result: None });
    }

    pub fn tool_result(&mut self, id: &str, ok: bool, summary: &str) {
        for e in self.entries.iter_mut().rev() {
            if let Item::Tool { id: i, result, .. } = &mut e.item {
                if i == id {
                    *result = Some((ok, if summary.is_empty() { if ok { "ok".into() } else { "failed".into() } } else { clean(summary) }));
                    e.dirty = true;
                    self.stale = true;
                    return;
                }
            }
        }
    }

    pub fn notice(&mut self, text: &str, error: bool) {
        self.drop_transient();
        self.end_stream();
        self.push(Item::Notice { text: clean(text), error });
    }

    /// A passing note (copy confirmation, quit hint). Never ends or splits a streaming reply: it goes in
    /// above the reply, which stays last so the next delta keeps appending to it.
    pub fn notice_quiet(&mut self, text: &str, error: bool) {
        self.drop_transient();
        let item = Item::Notice { text: clean(text), error };
        if matches!(self.entries.last(), Some(Entry { item: Item::Reply { streaming: true, .. }, .. })) {
            let at = self.entries.len() - 1;
            self.entries.insert(at, Entry { item, lines: Vec::new(), start: 0, dirty: true });
            self.stale = true;
        } else {
            self.push(item);
        }
    }

    pub fn end_turn(&mut self) {
        self.drop_transient();
        self.end_stream();
    }

    pub fn clear(&mut self) {
        self.entries.clear();
        self.sel.clear();
        (self.offset, self.follow, self.stale) = (0, true, true);
    }

    pub fn last_reply(&self) -> Option<&str> {
        self.entries.iter().rev().find_map(|e| match &e.item {
            Item::Reply { text, .. } => Some(text.as_str()),
            _ => None,
        })
    }

    pub fn toggle_cards(&mut self) {
        self.expanded = !self.expanded;
        self.sel.clear();
        for e in &mut self.entries {
            if matches!(e.item, Item::Tool { .. }) {
                e.dirty = true;
            }
        }
        self.stale = true;
    }

    // selection ----------------------------------------------------------------------------
    fn line_at(&self, i: usize) -> Option<&Line<'static>> {
        if self.stale {
            return None; // entry starts are only valid after a layout
        }
        let k = self.entries.partition_point(|e| e.start <= i).checked_sub(1)?;
        let e = &self.entries[k];
        e.lines.get(i - e.start)
    }

    /// The transcript line and column under a cell `(row, col)` of the transcript's own area.
    pub fn pos_at(&self, row: u16, col: u16) -> Option<Pos> {
        if self.total == 0 || self.stale {
            return None;
        }
        Some(((self.offset.min(self.max) + row as usize).min(self.total - 1), col as usize))
    }

    /// Button down: one click anchors a drag, two take a word, three a line.
    pub fn select_press(&mut self, p: Pos) {
        let n = self.sel.press(p, Instant::now());
        self.sel_start = self.entry_key(p.0);
        match n {
            2 => {
                if let Some(l) = self.line_at(p.0) {
                    let (a, b) = select::word_at(l, p.1);
                    self.sel.set((p.0, a), (p.0, b));
                }
            }
            3 => self.sel.set((p.0, 0), (p.0, select::EOL)),
            _ => {}
        }
    }

    /// (index, start line) of the entry holding `line`.
    fn entry_key(&self, line: usize) -> Option<(usize, usize)> {
        let k = self.entries.partition_point(|e| e.start <= line).saturating_sub(1);
        self.entries.get(k).map(|e| (k, e.start))
    }

    /// The selected text, lines joined by newlines.
    pub fn selected_text(&self) -> Option<String> {
        let ((l0, _), (l1, _)) = self.sel.ordered()?;
        let out: Vec<String> = (l0..=l1.min(self.total.saturating_sub(1)))
            .filter_map(|i| {
                let (f, t) = self.sel.cols(i)?;
                // code lines lose the display indent
                self.line_at(i).map(|l| select::slice(l, f.max(select::code_indent(l)), t))
            })
            .collect();
        let s = out.join("\n");
        let s = s.trim_matches('\n').to_owned();
        (!s.is_empty()).then_some(s)
    }

    // scroll -------------------------------------------------------------------------------
    fn scroll_blocked(&self, to_top: bool) -> bool {
        if self.blurred == Some(true) {
            return true; // blurred while pinned: nothing the reader did, keep the pin
        }
        to_top && self.guard_until.is_some_and(|(t, pinned)| pinned && t.elapsed() < REFOCUS_GUARD)
    }

    /// Positive = down. The result lands this call (1:1).
    pub fn scroll_by(&mut self, lines: i64) {
        if self.scroll_blocked(lines < 0 && self.offset <= lines.unsigned_abs() as usize) {
            return;
        }
        let at = self.offset.min(self.max) as i64 + lines;
        self.offset = at.clamp(0, self.max as i64) as usize;
        self.follow = self.offset >= self.max;
    }

    pub fn wheel(&mut self, down: bool) {
        self.scroll_by(if down { WHEEL_LINES as i64 } else { -(WHEEL_LINES as i64) });
    }

    pub fn page(&mut self, down: bool) {
        let p = (self.view_h as i64 - 2).max(1);
        self.scroll_by(if down { p } else { -p });
    }

    pub fn jump_top(&mut self) {
        if self.scroll_blocked(true) {
            return;
        }
        (self.offset, self.follow) = (0, self.max == 0);
    }

    pub fn jump_bottom(&mut self) {
        (self.offset, self.follow) = (self.max, true);
    }

    pub fn pinned(&self) -> bool {
        self.follow || self.max.saturating_sub(self.offset) < SLACK
    }

    pub fn blur(&mut self) {
        self.blurred = Some(self.pinned());
    }

    pub fn refocus(&mut self) {
        if let Some(pinned) = self.blurred.take() {
            if pinned {
                self.follow = true;
            }
            self.guard_until = Some((Instant::now(), pinned));
        }
    }

    // layout and paint -----------------------------------------------------------------------
    fn render_entry(item: &Item, th: &Theme, w: usize, expanded: bool, face_rows: usize) -> Vec<Line<'static>> {
        let g = th.glyphs;
        match item {
            Item::User(t) => {
                let first = [Span::styled(format!("{} ", g.prompt), th.user)];
                let rest = [Span::raw("  ")];
                let mut v = wrap::wrap(&markdown::inline(&t.replace('\n', " ⏎ "), th.user, th), w, &first, &rest);
                v.push(Line::default());
                v
            }
            Item::Reply { organ, text, .. } => {
                let mut v = markdown::render(text, th, w);
                if organ != "rabble" && !organ.is_empty() {
                    v.insert(0, Line::from(Span::styled(format!("{organ} {}", g.bullet), th.organ(organ))));
                }
                if v.is_empty() {
                    v.push(Line::default());
                }
                v.push(Line::default());
                v
            }
            Item::Face => vec![Line::default(); face_rows],
            Item::Thinking => vec![Line::from(Span::styled(format!("{}{}", "thinking", g.ellipsis), th.muted))],
            Item::Notice { text, error } => {
                let st = if *error { th.error } else { th.muted };
                let first = [Span::styled(format!("{} ", g.bullet), st)];
                let rest = [Span::raw("  ")];
                text.split('\n').flat_map(|l| wrap::wrap(&[Span::styled(l.to_owned(), st)], w, &first, &rest)).collect()
            }
            Item::Tool { organ, name, line, input, result, .. } => {
                let label = if organ == "rabble" || organ.is_empty() { String::new() } else { format!("{organ}: ") };
                let (rst, rtxt) = match result {
                    Some((true, s)) => (th.muted, format!(" {} {}", g.arrow, clip(s, RESULT_MAX, g.ellipsis))),
                    Some((false, s)) => (th.error, format!(" {} {}", g.arrow, clip(s, RESULT_MAX, g.ellipsis))),
                    None => (th.muted, String::new()),
                };
                let mark = if expanded { g.open } else { g.shut };
                let head = format!("  {mark} {label}{name}  {line}{rtxt}");
                let mut v = vec![Line::from(vec![
                    Span::styled(format!("  {mark} "), th.muted),
                    Span::styled(label, th.organ(organ)),
                    Span::styled(name.clone(), th.secondary),
                    Span::styled(format!("  {line}"), th.muted),
                    Span::styled(rtxt, rst),
                ])];
                if !expanded {
                    // one row: clip to the width
                    let cut = fit(&head, w, g.ellipsis);
                    if cut != head {
                        let st = v[0].spans.iter().map(|s| s.style).next().unwrap_or(Style::new());
                        v[0] = Line::from(Span::styled(cut, st));
                    }
                } else {
                    for l in input.lines() {
                        v.extend(wrap::wrap(&[Span::styled(l.to_owned(), th.muted)], w, &[Span::raw("      ")], &[Span::raw("        ")]));
                    }
                    if let Some((ok, s)) = result {
                        for l in s.lines() {
                            v.extend(wrap::wrap(&[Span::styled(l.to_owned(), if *ok { th.text } else { th.error })], w, &[Span::raw("    ")], &[Span::raw("    ")]));
                        }
                    }
                }
                v
            }
        }
    }

    /// Lines above the selection changed height (card toggled, result arrived, face resized): it drifted.
    fn guard_selection(&mut self) {
        if self.sel.has() {
            // keyed to the entry the press landed in (stable while the head wanders upward), else to the selection's first line
            let now = self.sel_start.map(|(k, _)| k).and_then(|k| self.entries.get(k).map(|e| (k, e.start)));
            match self.sel_start {
                Some(was) if Some(was) != now => {
                    self.sel.clear();
                    self.sel_start = None;
                }
                None => self.sel_start = self.sel.ordered().and_then(|(a, _)| self.entry_key(a.0)),
                _ => {}
            }
        } else {
            self.sel_start = None;
        }
    }

    /// Bring cached lines up to date for this width and height; apply the follow rules.
    pub fn layout(&mut self, th: &Theme, width: u16, height: u16) {
        let w = width.max(1) as usize;
        if width != self.width {
            self.width = width;
            self.stale = true;
            self.sel.clear();
            for e in &mut self.entries {
                e.dirty = true;
            }
        }
        self.view_h = height;
        let face_rows = (height as usize).max(FACE_MIN_ROWS);
        if face_rows != self.face_rows {
            self.face_rows = face_rows;
            self.stale = true;
            for e in &mut self.entries {
                if e.item == Item::Face {
                    e.dirty = true;
                }
            }
        }
        if !self.stale && self.max == self.total.saturating_sub(height as usize) {
            self.guard_selection();
            return;
        }
        let ex = self.expanded;
        let mut start = 0;
        for e in &mut self.entries {
            if e.dirty {
                e.lines = Self::render_entry(&e.item, th, w, ex, face_rows);
                e.dirty = false;
            }
            e.start = start;
            start += e.lines.len();
        }
        self.total = start;
        self.stale = false;
        let new_max = self.total.saturating_sub(height as usize);
        let prev_max = self.max;
        if self.follow {
            self.offset = new_max;
        } else if new_max > prev_max && prev_max.saturating_sub(self.offset) < SLACK {
            self.follow = true;
            self.offset = new_max;
        } else {
            self.offset = self.offset.min(new_max);
        }
        self.max = new_max;
        self.guard_selection();
    }

    pub fn draw(&self, area: Rect, buf: &mut Buffer) {
        if self.entries.is_empty() {
            return;
        }
        let top = self.offset.min(self.max);
        let first = self.entries.partition_point(|e| e.start <= top).saturating_sub(1);
        let mut y = 0u16;
        'outer: for e in &self.entries[first..] {
            let skip = top.saturating_sub(e.start);
            for l in e.lines.iter().skip(skip) {
                if y >= area.height {
                    break 'outer;
                }
                buf.set_line(area.x, area.y + y, l, area.width);
                if let Some((from, to)) = self.sel.cols(top + y as usize) {
                    for c in from..to.min(select::width(l)).min(area.width as usize) {
                        let cell = &mut buf[(area.x + c as u16, area.y + y)];
                        cell.modifier.insert(Modifier::REVERSED);
                    }
                }
                y += 1;
            }
        }
    }
}

pub struct View<'a>(pub &'a Transcript);

impl Widget for View<'_> {
    fn render(self, area: Rect, buf: &mut Buffer) {
        self.0.draw(area, buf)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::Glyphs;

    fn th() -> Theme {
        Theme::new(None, true, Glyphs::SAFE)
    }
    fn long() -> String {
        (0..120).map(|i| format!("paragraph {i} with some `code` text")).collect::<Vec<_>>().join("\n\n")
    }
    fn filled() -> Transcript {
        let mut t = Transcript::new();
        t.user("hi");
        t.delta("rabble", &long());
        t.layout(&th(), 80, 24);
        assert!(t.max_scroll() > 50 && t.offset == t.max_scroll());
        t
    }
    fn at_bottom(t: &Transcript) -> bool {
        t.max_scroll() - t.offset <= 1
    }
    fn paint(t: &mut Transcript, w: u16, h: u16) -> Vec<String> {
        t.layout(&th(), w, h);
        let mut b = Buffer::empty(Rect::new(0, 0, w, h));
        t.draw(Rect::new(0, 0, w, h), &mut b);
        (0..h).map(|y| (0..w).map(|x| b[(x, y)].symbol().to_owned()).collect::<String>().trim_end().to_owned()).collect()
    }

    #[test]
    fn text_is_drawn_once_when_the_block_follows_its_deltas() {
        let mut t = Transcript::new();
        t.delta("rabble", "Hel");
        t.delta("rabble", "lo");
        t.block("rabble", "Hello");
        assert_eq!(t.items().filter(|i| matches!(i, Item::Reply { .. })).count(), 1);
        assert_eq!(t.last_reply(), Some("Hello"));
        let rows = paint(&mut t, 20, 4);
        assert_eq!(rows.iter().filter(|r| r.contains("Hello")).count(), 1);
    }

    #[test]
    fn tool_card_title_survives_bracket_runs() {
        let mut t = Transcript::new();
        let input: serde_json::Map<_, _> = serde_json::from_str(r#"{"command":"print([Region(x=1), Region(y=2)])"}"#).unwrap();
        t.tool_call("rabble", "t1", "Bash", &input);
        t.tool_result("t1", true, "[Region(x=1), [..]]");
        let rows = paint(&mut t, 120, 3);
        assert!(rows[0].contains("Bash  print([Region(x=1), Region(y=2)])") && rows[0].contains("-> [Region(x=1), [..]]"), "{rows:?}");
    }

    #[test]
    fn tool_card_expands_to_the_input() {
        let mut t = Transcript::new();
        let input: serde_json::Map<_, _> = serde_json::from_str(r#"{"command":"ls","description":"d"}"#).unwrap();
        t.tool_call("gobble", "t1", "Bash", &input);
        t.tool_result("t1", false, "boom");
        t.toggle_cards();
        let rows = paint(&mut t, 60, 8).join("\n");
        assert!(rows.contains("gobble: Bash") && rows.contains("\"command\": \"ls\"") && rows.contains("boom"), "{rows}");
    }

    #[test]
    fn follows_while_pinned_and_stays_put_when_scrolled_up() {
        let mut t = filled();
        t.scroll_by(-(t.max_scroll() as i64 - 10)); // to line 10
        assert_eq!(t.offset, 10);
        for i in 0..10 {
            t.delta("rabble", &format!("\n\nmore {i}"));
            t.layout(&th(), 80, 24);
        }
        assert_eq!(t.offset, 10);
        assert!(!t.follow);
    }

    #[test]
    fn nudged_two_lines_up_still_follows_but_a_notch_does_not_get_dragged_after_a_long_read() {
        let mut t = filled();
        t.scroll_by(-2);
        assert!(!t.follow && t.pinned());
        t.block("rabble", &(long() + "\n\nfinal"));
        t.layout(&th(), 80, 24);
        assert!(at_bottom(&t));
    }

    #[test]
    fn submit_jumps_to_bottom_after_scrolling_up() {
        let mut t = filled();
        t.scroll_by(-100);
        t.user("again");
        t.layout(&th(), 80, 24);
        assert!(at_bottom(&t));
    }

    #[test]
    fn stray_jump_to_top_while_blurred_is_repinned() {
        let mut t = filled();
        t.blur();
        t.jump_top();
        t.scroll_by(-500);
        t.layout(&th(), 80, 24);
        assert!(at_bottom(&t));
        for i in 0..5 {
            t.delta("rabble", &format!("\n\nmore {i}"));
        }
        t.refocus();
        t.layout(&th(), 80, 24);
        assert!(at_bottom(&t));
    }

    #[test]
    fn stray_jump_right_after_refocus_is_repinned() {
        let mut t = filled();
        t.blur();
        t.refocus();
        t.jump_top();
        t.layout(&th(), 80, 24);
        assert!(at_bottom(&t));
        std::thread::sleep(REFOCUS_GUARD + Duration::from_millis(30));
        t.jump_top();
        assert_eq!(t.offset, 0, "after the guard the reader may scroll");
    }

    #[test]
    fn a_reader_scrolled_up_stays_up_across_blur() {
        let mut t = filled();
        t.scroll_by(-40);
        let at = t.offset;
        t.blur();
        t.refocus();
        t.jump_top();
        assert_eq!(t.offset, 0, "not pinned: no guard");
        let _ = at;
    }

    #[test]
    fn wheel_is_one_to_one_three_lines_per_notch() {
        let mut t = filled();
        let top = t.offset;
        for _ in 0..4 {
            t.wheel(false);
        }
        assert_eq!(t.offset, top - 4 * WHEEL_LINES);
        t.wheel(true);
        assert_eq!(t.offset, top - 3 * WHEEL_LINES);
        t.jump_bottom();
        assert!(t.follow);
    }

    #[test]
    fn resize_rewraps_and_keeps_the_bottom() {
        let mut t = filled();
        let before = t.total_lines();
        t.layout(&th(), 30, 10);
        assert!(t.total_lines() > before && at_bottom(&t));
        t.layout(&th(), 200, 60);
        assert!(at_bottom(&t));
    }

    #[test]
    fn thinking_placeholder_goes_away_on_content() {
        let mut t = Transcript::new();
        t.user("q");
        t.thinking();
        t.thinking();
        assert_eq!(t.len(), 2);
        t.delta("rabble", "a");
        assert!(t.items().all(|i| *i != Item::Thinking));
    }

    #[test]
    fn notices_and_errors_show_and_unicode_does_not_overflow() {
        let mut t = Transcript::new();
        t.notice("日本語日本語日本語 more", true);
        let rows = paint(&mut t, 10, 5);
        let all: String = rows.concat().chars().filter(|c| !c.is_whitespace()).collect();
        assert_eq!(all, "*日本語日本語日本語more", "{rows:?}"); // nothing lost, nothing clipped at width 10
    }

    #[test]
    fn escape_sequences_from_the_engine_never_reach_a_cell() {
        let mut t = Transcript::new();
        let evil = "a\x1b]52;c;QQ==\x07b\x1b[2Jc \u{202e}rtl \u{9b}d\te";
        t.user(evil);
        t.delta("rab\x1bble", evil);
        t.end_stream();
        let input: serde_json::Map<_, _> = serde_json::from_str(&serde_json::json!({"command": evil}).to_string()).unwrap();
        t.tool_call("rabble", "t", "Ba\x1bsh", &input);
        t.tool_result("t", true, evil);
        t.notice(evil, true);
        t.toggle_cards();
        let rows = paint(&mut t, 80, 40);
        let b: String = rows.concat();
        assert!(!b.chars().any(char::is_control) && !b.contains('\u{202e}'), "{rows:?}");
        assert!(b.contains("d    e"), "tabs are expanded before wrapping");
    }

    fn select_all(t: &mut Transcript) {
        t.sel.set((0, 0), (t.total_lines() - 1, select::EOL));
    }

    #[test]
    fn a_quiet_notice_does_not_split_a_streaming_reply() {
        let mut t = Transcript::new();
        t.delta("rabble", "first half ");
        t.notice_quiet("copied", false);
        t.delta("rabble", "second half");
        t.block("rabble", "first half second half");
        t.layout(&th(), 80, 24);
        let replies: Vec<_> = t.items().filter_map(|i| if let Item::Reply { text, .. } = i { Some(text.clone()) } else { None }).collect();
        assert_eq!(replies, ["first half second half"], "one reply, text once");
        assert!(t.items().any(|i| matches!(i, Item::Notice { text, .. } if text == "copied")));
    }

    #[test]
    fn copying_code_drops_the_display_indent_but_keeps_its_own() {
        let mut t = Transcript::new();
        t.block("rabble", "```\nfn a() {\n    b();\n}\n```");
        t.layout(&th(), 80, 24);
        select_all(&mut t);
        assert_eq!(t.selected_text().as_deref(), Some("fn a() {\n    b();\n}"));
    }

    #[test]
    fn a_selection_is_dropped_when_lines_above_it_change_height() {
        let mut t = Transcript::new();
        t.tool_call("rabble", "t1", "Bash", &serde_json::Map::new());
        t.block("rabble", "alpha");
        t.layout(&th(), 80, 24);
        let row = t.total_lines() - 2;
        t.sel.set((row, 0), (row, 4));
        t.layout(&th(), 80, 24);
        assert!(t.sel.has(), "a steady layout keeps it");
        t.tool_result("t1", true, "x");
        t.expanded = true;
        t.toggle_cards();
        t.sel.set((row, 0), (row, 4));
        t.layout(&th(), 80, 24);
        t.tool_call("rabble", "t0", "Bash", &serde_json::Map::new());
        // growth below does not matter
        t.layout(&th(), 80, 24);
        assert!(t.sel.has());
        t.toggle_cards(); // clears directly, then a new selection must survive until lines above move
        t.layout(&th(), 80, 24);
        let row = t.total_lines() - 3;
        t.sel.set((row, 0), (row, 2));
        t.layout(&th(), 80, 24);
        t.tool_result("t0", true, "a\nb\nc");
        t.toggle_cards();
        t.toggle_cards();
        t.layout(&th(), 80, 24);
        assert!(!t.sel.has());
    }
}
