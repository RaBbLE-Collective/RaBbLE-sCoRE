//! The input box: multiline editing, bracketed paste, history (Up on the first line, Down on the
//! last), `/command` completion (the list comes from `Capabilities`) and `@path` completion.
//! Keys are interpreted here and returned as an `Action`; the app decides what they mean.

use std::path::Path;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{buffer::Buffer, layout::Rect, text::{Line, Span}};

use crate::{theme::Theme, wrap::cw};

pub const MAX_ROWS: usize = 8;
pub const SHOW: usize = 8;
const SCAN_CAP: usize = 5000;
const MAX_DEPTH: usize = 6;
const SKIP: [&str; 7] = [".git", "node_modules", ".venv", "__pycache__", ".cache", "dist", "build"];

#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    None,
    Submit(String),
    Interrupt,
    CycleMode,
    CtrlC,
    CtrlD,
    Paste, // the app should fetch the system clipboard and call `insert_str`
}

pub fn scan_files(root: &Path, cap: usize) -> Vec<String> {
    let mut out = Vec::new();
    let mut stack = vec![(root.to_path_buf(), 0usize)];
    while let Some((dir, depth)) = stack.pop() {
        let Ok(rd) = std::fs::read_dir(&dir) else { continue };
        let mut names: Vec<(String, bool)> = rd.filter_map(|e| e.ok()).map(|e| (e.file_name().to_string_lossy().into_owned(), e.path().is_dir())).collect();
        names.sort();
        let mut subdirs = Vec::new();
        for (n, is_dir) in names {
            let rel = dir.strip_prefix(root).unwrap_or(&dir).join(&n).to_string_lossy().into_owned();
            if is_dir {
                if SKIP.contains(&n.as_str()) || (n.starts_with('.') && n != ".claude") {
                    continue;
                }
                out.push(format!("{rel}/"));
                if depth + 1 < MAX_DEPTH {
                    subdirs.push((dir.join(&n), depth + 1));
                }
            } else {
                out.push(rel);
            }
        }
        stack.extend(subdirs.into_iter().rev());
        if out.len() >= cap {
            break;
        }
    }
    out.truncate(cap);
    out
}

pub fn match_files(files: &[String], frag: &str, limit: usize) -> Vec<String> {
    let frag = frag.to_lowercase();
    let base = |f: &str| f.trim_end_matches('/').rsplit('/').next().unwrap_or("").to_lowercase();
    let mut hits: Vec<&String> = files.iter().filter(|f| f.to_lowercase().contains(&frag)).collect();
    hits.sort_by_key(|f| (!f.to_lowercase().starts_with(&frag), !base(f).starts_with(&frag), f.len()));
    hits.into_iter().take(limit).cloned().collect()
}

pub struct Composer {
    buf: Vec<char>,
    cur: usize,
    history: Vec<String>,
    pos: Option<usize>,
    draft: String,
    pub commands: Vec<String>,
    cwd: String,
    files: Option<Vec<String>>,
    pub dropdown: Vec<String>,
    pub sel: usize,
    slash: bool,
    frag_len: usize,
}

impl Composer {
    pub fn new(cwd: &str, commands: Vec<String>) -> Self {
        Composer { buf: Vec::new(), cur: 0, history: Vec::new(), pos: None, draft: String::new(), commands, cwd: cwd.into(), files: None, dropdown: Vec::new(), sel: 0, slash: false, frag_len: 0 }
    }

    pub fn text(&self) -> String {
        self.buf.iter().collect()
    }
    pub fn is_empty(&self) -> bool {
        self.buf.is_empty()
    }
    pub fn completing(&self) -> bool {
        !self.dropdown.is_empty()
    }
    pub fn history(&self) -> &[String] {
        &self.history
    }

    pub fn set_text(&mut self, t: &str) {
        self.buf = t.chars().collect();
        self.cur = self.buf.len();
        self.update_completion();
    }

    fn row_col(&self) -> (usize, usize) {
        let before = &self.buf[..self.cur];
        let row = before.iter().filter(|&&c| c == '\n').count();
        let col = before.iter().rev().take_while(|&&c| c != '\n').count();
        (row, col)
    }

    fn line_count(&self) -> usize {
        self.buf.iter().filter(|&&c| c == '\n').count() + 1
    }

    pub fn insert_str(&mut self, s: &str) {
        let s = s.replace("\r\n", "\n").replace('\r', "\n");
        for c in s.chars() {
            self.buf.insert(self.cur, c);
            self.cur += 1;
        }
        self.pos = None;
        self.update_completion();
    }

    fn line_start(&self, i: usize) -> usize {
        self.buf[..i].iter().rposition(|&c| c == '\n').map_or(0, |p| p + 1)
    }
    fn line_end(&self, i: usize) -> usize {
        self.buf[i..].iter().position(|&c| c == '\n').map_or(self.buf.len(), |p| i + p)
    }

    fn update_completion(&mut self) {
        self.dropdown.clear();
        self.sel = 0;
        self.slash = false;
        let before: String = self.buf[..self.cur].iter().collect();
        let (row, _) = self.row_col();
        if row == 0 && before.starts_with('/') && !before.chars().any(char::is_whitespace) && !self.commands.is_empty() {
            let b = before.to_lowercase();
            let hits: Vec<String> = self.commands.iter().filter(|c| c.starts_with(&b)).cloned().collect();
            if !hits.is_empty() && hits != [before.clone()] {
                self.slash = true;
                self.dropdown = hits;
            }
            return;
        }
        // @fragment at the end of the text before the cursor
        let frag_start = before.rfind(|c: char| c.is_whitespace()).map_or(0, |p| p + 1);
        let word = &before[frag_start..];
        if let Some(frag) = word.strip_prefix('@') {
            if !frag.contains('@') {
                let files = self.files.get_or_insert_with(|| scan_files(Path::new(&self.cwd), SCAN_CAP));
                self.frag_len = frag.chars().count();
                self.dropdown = match_files(files, frag, SHOW);
            }
        }
    }

    fn accept(&mut self) {
        let Some(pick) = self.dropdown.get(self.sel).cloned() else { return };
        if self.slash {
            self.buf.drain(..self.cur);
            let new: Vec<char> = format!("{pick} ").chars().collect();
            let n = new.len();
            self.buf.splice(0..0, new);
            self.cur = n;
        } else {
            let from = self.cur - self.frag_len;
            self.buf.drain(from..self.cur);
            let ins = if pick.ends_with('/') { pick } else { format!("{pick} ") };
            let new: Vec<char> = ins.chars().collect();
            let n = new.len();
            self.buf.splice(from..from, new);
            self.cur = from + n;
        }
        self.dropdown.clear();
        self.slash = false;
    }

    fn recall(&mut self, step: isize) {
        if self.history.is_empty() {
            return;
        }
        let pos = match self.pos {
            Some(p) => p,
            None => {
                self.draft = self.text();
                self.history.len()
            }
        };
        let pos = (pos as isize + step).clamp(0, self.history.len() as isize) as usize;
        self.pos = Some(pos);
        let t = if pos == self.history.len() { self.draft.clone() } else { self.history[pos].clone() };
        self.buf = t.chars().collect();
        self.cur = self.buf.len();
        self.dropdown.clear();
    }

    pub fn key(&mut self, k: KeyEvent) -> Action {
        let ctrl = k.modifiers.contains(KeyModifiers::CONTROL);
        let alt = k.modifiers.contains(KeyModifiers::ALT);
        let shift = k.modifiers.contains(KeyModifiers::SHIFT);
        match k.code {
            KeyCode::Enter if shift || alt => self.insert_str("\n"),
            KeyCode::Char('j') if ctrl => self.insert_str("\n"),
            KeyCode::Enter if self.completing() && self.slash => {
                self.accept();
                self.update_completion();
            }
            KeyCode::Enter => {
                let text = self.text().trim().to_owned();
                self.dropdown.clear();
                if text.is_empty() {
                    return Action::None;
                }
                if self.history.last() != Some(&text) {
                    self.history.push(text.clone());
                }
                self.buf.clear();
                self.cur = 0;
                self.pos = None;
                return Action::Submit(text);
            }
            KeyCode::Tab if self.completing() => {
                self.accept();
                self.update_completion();
            }
            KeyCode::Esc => {
                if self.completing() {
                    self.dropdown.clear();
                } else {
                    return Action::Interrupt;
                }
            }
            KeyCode::BackTab => return Action::CycleMode,
            KeyCode::Char('c') if ctrl => return Action::CtrlC,
            KeyCode::Char('d') if ctrl && self.buf.is_empty() => return Action::CtrlD,
            KeyCode::Char('v') if ctrl => return Action::Paste,
            KeyCode::Char('a') if ctrl => self.cur = self.line_start(self.cur),
            KeyCode::Char('e') if ctrl => self.cur = self.line_end(self.cur),
            KeyCode::Char('u') if ctrl => {
                let s = self.line_start(self.cur);
                self.buf.drain(s..self.cur);
                self.cur = s;
                self.update_completion();
            }
            KeyCode::Char('k') if ctrl => {
                let e = self.line_end(self.cur);
                self.buf.drain(self.cur..e);
            }
            KeyCode::Char('w') if ctrl => {
                let mut s = self.cur;
                while s > 0 && self.buf[s - 1].is_whitespace() {
                    s -= 1;
                }
                while s > 0 && !self.buf[s - 1].is_whitespace() {
                    s -= 1;
                }
                self.buf.drain(s..self.cur);
                self.cur = s;
                self.update_completion();
            }
            KeyCode::Up if self.completing() => self.sel = self.sel.saturating_sub(1),
            KeyCode::Down if self.completing() => self.sel = (self.sel + 1).min(self.dropdown.len() - 1),
            KeyCode::Up => {
                let (row, col) = self.row_col();
                if row == 0 {
                    self.recall(-1);
                } else {
                    let ps = self.line_start(self.line_start(self.cur).saturating_sub(1));
                    let plen = self.line_start(self.cur) - 1 - ps;
                    self.cur = ps + col.min(plen);
                }
            }
            KeyCode::Down => {
                let (row, col) = self.row_col();
                if row + 1 >= self.line_count() {
                    if self.pos.is_some() {
                        self.recall(1);
                    }
                } else {
                    let ns = self.line_end(self.cur) + 1;
                    let nlen = self.line_end(ns) - ns;
                    self.cur = ns + col.min(nlen);
                }
            }
            KeyCode::Left => self.cur = self.cur.saturating_sub(1),
            KeyCode::Right => self.cur = (self.cur + 1).min(self.buf.len()),
            KeyCode::Home => self.cur = self.line_start(self.cur),
            KeyCode::End => self.cur = self.line_end(self.cur),
            KeyCode::Backspace if self.cur > 0 => {
                self.cur -= 1;
                self.buf.remove(self.cur);
                self.pos = None;
                self.update_completion();
            }
            KeyCode::Delete if self.cur < self.buf.len() => {
                self.buf.remove(self.cur);
                self.update_completion();
            }
            KeyCode::Char(c) if !ctrl && !alt => {
                self.buf.insert(self.cur, c);
                self.cur += 1;
                self.pos = None;
                self.update_completion();
            }
            _ => {}
        }
        Action::None
    }

    /// Wrap to `width` cells per row. Returns the rows and the cursor (row, col) among them.
    fn rows(&self, width: usize) -> (Vec<String>, (usize, usize)) {
        let width = width.max(2);
        let (mut rows, mut cur_row, mut col) = (vec![String::new()], 0usize, 0usize);
        let mut cursor = (0, 0);
        for i in 0..=self.buf.len() {
            if i == self.cur {
                // a char that will not fit moves to the next row first
                let next_w = self.buf.get(i).filter(|&&c| c != '\n').map_or(1, |&c| cw(c).max(1));
                if col + next_w > width && i < self.buf.len() && self.buf[i] != '\n' {
                    rows.push(String::new());
                    cur_row += 1;
                    col = 0;
                }
                cursor = (cur_row, col);
            }
            let Some(&c) = self.buf.get(i) else { break };
            if c == '\n' {
                rows.push(String::new());
                cur_row += 1;
                col = 0;
                continue;
            }
            let w = cw(c);
            if col + w > width {
                rows.push(String::new());
                cur_row += 1;
                col = 0;
            }
            rows[cur_row].push(c);
            col += w;
        }
        (rows, cursor)
    }

    /// Rows the box needs at this width (including its border).
    pub fn height(&self, width: u16) -> u16 {
        let (rows, _) = self.rows((width as usize).saturating_sub(4));
        rows.len().clamp(1, MAX_ROWS) as u16 + 2
    }

    /// Draw the bordered box; returns the cursor position inside `area`.
    pub fn draw(&self, area: Rect, buf: &mut Buffer, th: &Theme, focused: bool) -> (u16, u16) {
        let g = th.glyphs;
        let st = if focused { th.tertiary } else { th.border };
        let w = area.width as usize;
        if w < 6 || area.height < 3 {
            return (area.x, area.y);
        }
        let inner = w - 4;
        let bar = |l: char, r: char| Line::from(Span::styled(format!("{l}{}{r}", g.rule.to_string().repeat(w - 2)), st));
        buf.set_line(area.x, area.y, &bar(g.corner[0], g.corner[1]), area.width);
        buf.set_line(area.x, area.y + area.height - 1, &bar(g.corner[2], g.corner[3]), area.width);
        let (rows, (cr, cc)) = self.rows(inner);
        let vis = (area.height - 2) as usize;
        let first = (cr + 1).saturating_sub(vis);
        for i in 0..vis {
            let y = area.y + 1 + i as u16;
            let text = rows.get(first + i).map(String::as_str).unwrap_or("");
            let line = Line::from(vec![Span::styled(g.vert.to_string(), st), Span::raw(" "), Span::styled(text.to_owned(), th.text)]);
            buf.set_line(area.x, y, &line, area.width);
            buf.set_line(area.x + area.width - 1, y, &Line::from(Span::styled(g.vert.to_string(), st)), 1);
        }
        (area.x + 2 + cc as u16, area.y + 1 + (cr - first) as u16)
    }

    pub fn draw_dropdown(&self, area: Rect, buf: &mut Buffer, th: &Theme) {
        let n = self.dropdown.len().min(area.height as usize);
        let first = self.sel.saturating_sub(n.saturating_sub(1));
        for (i, item) in self.dropdown.iter().skip(first).take(n).enumerate() {
            let st = if first + i == self.sel { th.select } else { th.text };
            let y = area.y + area.height - n as u16 + i as u16;
            buf.set_line(area.x, y, &Line::from(Span::styled(format!(" {item:<w$}", w = area.width as usize - 1), st)), area.width);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn k(c: KeyCode) -> KeyEvent {
        KeyEvent::new(c, KeyModifiers::NONE)
    }
    fn typ(c: &mut Composer, s: &str) {
        for ch in s.chars() {
            c.key(k(KeyCode::Char(ch)));
        }
    }
    fn comp() -> Composer {
        Composer::new("/nonexistent", vec!["/clear".into(), "/compact".into(), "/model".into(), "/mode".into()])
    }

    #[test]
    fn enter_submits_trimmed_shift_enter_adds_a_line() {
        let mut c = comp();
        typ(&mut c, "hello ");
        c.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT));
        typ(&mut c, "world  ");
        assert_eq!(c.text(), "hello \nworld  ");
        assert_eq!(c.key(k(KeyCode::Enter)), Action::Submit("hello \nworld".into()));
        assert!(c.is_empty());
        assert_eq!(c.key(k(KeyCode::Enter)), Action::None, "empty does not submit");
    }

    #[test]
    fn paste_keeps_newlines_and_normalises_crlf() {
        let mut c = comp();
        c.insert_str("a\r\nb\rc");
        assert_eq!(c.text(), "a\nb\nc");
    }

    #[test]
    fn history_up_on_first_line_down_returns_to_the_draft() {
        let mut c = comp();
        for s in ["one", "two"] {
            typ(&mut c, s);
            c.key(k(KeyCode::Enter));
        }
        typ(&mut c, "dr");
        c.key(k(KeyCode::Up));
        assert_eq!(c.text(), "two");
        c.key(k(KeyCode::Up));
        assert_eq!(c.text(), "one");
        c.key(k(KeyCode::Down));
        c.key(k(KeyCode::Down));
        assert_eq!(c.text(), "dr");
    }

    #[test]
    fn up_inside_multiline_moves_the_cursor_not_history() {
        let mut c = comp();
        c.history.push("old".into());
        c.insert_str("ab\ncd");
        c.key(k(KeyCode::Up));
        assert_eq!(c.text(), "ab\ncd");
        assert_eq!(c.row_col(), (0, 2));
    }

    #[test]
    fn slash_completion_lists_matches_and_enter_accepts() {
        let mut c = comp();
        typ(&mut c, "/mo");
        assert_eq!(c.dropdown, ["/model", "/mode"]);
        c.key(k(KeyCode::Down));
        assert_eq!(c.key(k(KeyCode::Enter)), Action::None);
        assert_eq!(c.text(), "/mode ");
        assert!(!c.completing());
    }

    #[test]
    fn slash_exact_match_and_later_words_do_not_complete() {
        let mut c = comp();
        typ(&mut c, "/clear");
        assert!(!c.completing());
        typ(&mut c, " x");
        assert!(!c.completing());
        c.set_text("not /mo");
        assert!(!c.completing());
    }

    #[test]
    fn escape_closes_the_list_then_interrupts() {
        let mut c = comp();
        typ(&mut c, "/c");
        assert_eq!(c.key(k(KeyCode::Esc)), Action::None);
        assert_eq!(c.key(k(KeyCode::Esc)), Action::Interrupt);
    }

    #[test]
    fn at_completion_finds_files() {
        let dir = std::env::temp_dir().join(format!("rt-at-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("sub")).unwrap();
        std::fs::write(dir.join("sub/notes.md"), "x").unwrap();
        std::fs::create_dir_all(dir.join("node_modules/x")).unwrap();
        let mut c = Composer::new(dir.to_str().unwrap(), vec![]);
        typ(&mut c, "see @not");
        assert_eq!(c.dropdown, ["sub/notes.md"]);
        c.key(k(KeyCode::Tab));
        assert_eq!(c.text(), "see @sub/notes.md ");
        assert!(scan_files(&dir, 100).iter().all(|f| !f.contains("node_modules")));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn control_keys_become_actions() {
        let mut c = comp();
        assert_eq!(c.key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)), Action::CtrlC);
        assert_eq!(c.key(KeyEvent::new(KeyCode::Char('d'), KeyModifiers::CONTROL)), Action::CtrlD);
        typ(&mut c, "x");
        assert_eq!(c.key(KeyEvent::new(KeyCode::Char('d'), KeyModifiers::CONTROL)), Action::None, "Ctrl-D only quits an empty box");
        assert_eq!(c.key(k(KeyCode::BackTab)), Action::CycleMode);
        assert_eq!(c.key(KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL)), Action::Paste);
    }

    #[test]
    fn editing_keys() {
        let mut c = comp();
        typ(&mut c, "abc def");
        c.key(KeyEvent::new(KeyCode::Char('w'), KeyModifiers::CONTROL));
        assert_eq!(c.text(), "abc ");
        c.key(k(KeyCode::Left));
        c.key(k(KeyCode::Backspace));
        assert_eq!(c.text(), "ab ");
        c.key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL));
        assert_eq!(c.text(), " ");
    }

    #[test]
    fn wrapping_tracks_the_cursor_and_height_is_capped() {
        let mut c = comp();
        c.insert_str(&"x".repeat(25));
        let (rows, cur) = c.rows(10);
        assert_eq!((rows.len(), cur), (3, (2, 5)));
        c.insert_str(&"\n".repeat(30));
        assert_eq!(c.height(80), MAX_ROWS as u16 + 2);
    }
}
