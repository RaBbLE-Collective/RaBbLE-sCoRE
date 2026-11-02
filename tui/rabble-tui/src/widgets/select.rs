//! In-app text selection for the transcript. Positions are `(line, column)` in the transcript's own wrapped
//! lines (display columns, so wide characters count two), which keeps a selection put while the view scrolls
//! or a reply streams in below it. Press starts a drag, release keeps the range, a double click takes a word,
//! a triple click a line. The transcript paints the range reversed and `text` reads it back for Ctrl-C.

use std::time::{Duration, Instant};

use ratatui::text::Line;

use crate::wrap::cw;

pub type Pos = (usize, usize);
/// "To the end of the line" for line selections.
pub const EOL: usize = usize::MAX / 2;
const MULTI_CLICK: Duration = Duration::from_millis(400);

#[derive(Debug, Default)]
pub struct Selection {
    anchor: Option<Pos>,
    range: Option<(Pos, Pos)>, // inclusive of the head cell
    last_click: Option<(Instant, Pos, u8)>,
}

impl Selection {
    pub fn clear(&mut self) {
        self.anchor = None;
        self.range = None;
    }

    pub fn has(&self) -> bool {
        self.range.is_some()
    }

    /// Button pressed at `p`; returns the click count (1, 2, 3) so the caller can widen to a word or line.
    pub fn press(&mut self, p: Pos, now: Instant) -> u8 {
        let n = match self.last_click {
            Some((t, q, n)) if q == p && now.duration_since(t) <= MULTI_CLICK => n % 3 + 1,
            _ => 1,
        };
        self.last_click = Some((now, p, n));
        self.range = None;
        self.anchor = Some(p);
        n
    }

    pub fn set(&mut self, a: Pos, b: Pos) {
        self.range = Some((a, b));
    }

    pub fn drag(&mut self, head: Pos) {
        if let Some(a) = self.anchor {
            if a != head || self.range.is_some() {
                self.range = Some((a, head));
            }
        }
    }

    pub fn release(&mut self) {
        self.anchor = None;
    }

    /// Start and end in reading order.
    pub fn ordered(&self) -> Option<(Pos, Pos)> {
        self.range.map(|(a, b)| if a <= b { (a, b) } else { (b, a) })
    }

    /// The columns `[from, to)` selected on `line`, if any.
    pub fn cols(&self, line: usize) -> Option<(usize, usize)> {
        let (a, b) = self.ordered()?;
        if line < a.0 || line > b.0 {
            return None;
        }
        Some((if line == a.0 { a.1 } else { 0 }, if line == b.0 { b.1.saturating_add(1) } else { usize::MAX }))
    }
}

/// `(char, first column, width)` for every character of a line.
fn cells(l: &Line) -> Vec<(char, usize, usize)> {
    let mut col = 0;
    let mut out = Vec::new();
    for s in &l.spans {
        for c in s.content.chars() {
            let w = cw(c);
            out.push((c, col, w));
            col += w;
        }
    }
    out
}

pub fn width(l: &Line) -> usize {
    cells(l).last().map_or(0, |&(_, c, w)| c + w)
}

/// The text of columns `[from, to)`, a wide character included whole if it overlaps; trailing blanks dropped.
pub fn slice(l: &Line, from: usize, to: usize) -> String {
    let s: String = cells(l).into_iter().filter(|&(_, c, w)| w > 0 && c < to && c + w > from).map(|(ch, ..)| ch).collect();
    s.trim_end().to_owned()
}

/// The start and end column (inclusive) of the run of non-blank characters under `col`, or the cell itself
/// when it is blank.
pub fn word_at(l: &Line, col: usize) -> (usize, usize) {
    let cs = cells(l);
    let Some(i) = cs.iter().position(|&(_, c, w)| col >= c && col < c + w.max(1)) else { return (col, col) };
    if cs[i].0.is_whitespace() {
        return (cs[i].1, cs[i].1);
    }
    let (mut a, mut b) = (i, i);
    while a > 0 && !cs[a - 1].0.is_whitespace() {
        a -= 1;
    }
    while b + 1 < cs.len() && !cs[b + 1].0.is_whitespace() {
        b += 1;
    }
    (cs[a].1, cs[b].1 + cs[b].2.saturating_sub(1))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn l(s: &str) -> Line<'static> {
        Line::from(s.to_owned())
    }

    #[test]
    fn slice_counts_display_columns_and_keeps_wide_chars_whole() {
        assert_eq!(slice(&l("hello world"), 6, 11), "world");
        assert_eq!(slice(&l("日本語 ok"), 2, 6), "本語");
        assert_eq!(slice(&l("日本語"), 1, 2), "日", "a column inside a wide char takes the whole char");
        assert_eq!(slice(&l("ab   "), 0, 99), "ab", "padding is not copied");
    }

    #[test]
    fn a_word_is_the_run_of_non_blanks() {
        let line = l("run ~/a/b.rs now");
        assert_eq!(word_at(&line, 6), (4, 11));
        assert_eq!(word_at(&line, 3), (3, 3), "a blank selects itself");
        assert_eq!(word_at(&line, 99), (99, 99));
    }

    #[test]
    fn drag_orders_either_way_and_a_bare_click_selects_nothing() {
        let mut s = Selection::default();
        let t = Instant::now();
        s.press((5, 3), t);
        s.drag((5, 3));
        assert!(!s.has());
        s.drag((2, 8));
        assert_eq!(s.ordered(), Some(((2, 8), (5, 3))));
        assert_eq!(s.cols(2), Some((8, usize::MAX)));
        assert_eq!(s.cols(3), Some((0, usize::MAX)));
        assert_eq!(s.cols(5), Some((0, 4)));
        assert_eq!(s.cols(6), None);
        s.release();
        s.clear();
        assert!(!s.has());
    }

    #[test]
    fn clicks_in_place_within_the_window_count_up_and_wrap() {
        let mut s = Selection::default();
        let t = Instant::now();
        assert_eq!(s.press((1, 1), t), 1);
        assert_eq!(s.press((1, 1), t + Duration::from_millis(100)), 2);
        assert_eq!(s.press((1, 1), t + Duration::from_millis(200)), 3);
        assert_eq!(s.press((1, 1), t + Duration::from_millis(300)), 1);
        assert_eq!(s.press((1, 2), t + Duration::from_millis(350)), 1, "a different cell restarts");
        assert_eq!(s.press((1, 2), t + Duration::from_secs(2)), 1, "too slow restarts");
    }
}
