//! Word wrap for styled text, by display width (CJK and emoji count two cells, combining marks
//! none). Greedy: break after the last space that fits, else hard-break inside the word.

use ratatui::{style::Style, text::{Line, Span}};
use unicode_width::UnicodeWidthChar;

pub fn cw(c: char) -> usize {
    c.width().unwrap_or(0)
}

pub fn str_width(s: &str) -> usize {
    s.chars().map(cw).sum()
}

pub fn spans_width(spans: &[Span]) -> usize {
    spans.iter().map(|s| str_width(&s.content)).sum()
}

/// Text from outside (the model, a tool, a file name) made safe to put in a terminal cell:
/// control characters (so no escape sequence reaches the terminal), C1 controls, DEL and bidi
/// overrides are dropped; tabs become four spaces; newlines are kept for the caller to split.
pub fn clean(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '\n' => out.push('\n'),
            '\t' => out.push_str("    "),
            c if c.is_control() => {}
            '\u{200e}' | '\u{200f}' | '\u{061c}' | '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}' => {}
            c => out.push(c),
        }
    }
    out
}

type Cell = (char, Style);

fn flatten(spans: &[Span<'_>]) -> Vec<Cell> {
    spans.iter().flat_map(|s| s.content.chars().map(move |c| (c, s.style))).collect()
}

fn group(cells: &[Cell], prefix: &[Span<'static>]) -> Line<'static> {
    let mut spans: Vec<Span<'static>> = prefix.to_vec();
    let mut buf = String::new();
    let mut cur: Option<Style> = None;
    for &(c, st) in cells {
        if cur != Some(st) {
            if let Some(s) = cur {
                spans.push(Span::styled(std::mem::take(&mut buf), s));
            }
            cur = Some(st);
        }
        buf.push(c);
    }
    if let Some(s) = cur {
        spans.push(Span::styled(buf, s));
    }
    Line::from(spans)
}

/// Wrap `spans` to `width`. `first` prefixes the first line, `rest` every continuation line
/// (hanging indent, quote bars). An empty input still yields one (prefixed) line.
pub fn wrap(spans: &[Span<'_>], width: usize, first: &[Span<'static>], rest: &[Span<'static>]) -> Vec<Line<'static>> {
    let cells = flatten(spans);
    let mut out = Vec::new();
    let mut start = 0;
    let mut prefix = first;
    loop {
        let room = width.saturating_sub(spans_width(prefix)).max(1);
        let (mut col, mut i, mut brk) = (0usize, start, None::<usize>);
        let mut end = cells.len();
        let mut next = cells.len();
        while i < cells.len() {
            let w = cw(cells[i].0);
            if col + w > room && i > start {
                if let Some(b) = brk {
                    end = b; // line ends before the space at b
                    next = b + 1;
                } else {
                    end = i;
                    next = i;
                }
                break;
            }
            if cells[i].0 == ' ' && i > start {
                brk = Some(i);
            }
            col += w;
            i += 1;
        }
        let mut line = group(&cells[start..end], prefix);
        if next == cells.len() && end == cells.len() {
            line.spans.retain(|s| !s.content.is_empty());
            out.push(line);
            return out;
        }
        // keep leading spaces of the continuation only when a hard break split a word
        out.push(line);
        start = next;
        while start < cells.len() && cells[start].0 == ' ' && next != end {
            start += 1;
        }
        prefix = rest;
        if start >= cells.len() {
            return out;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(l: &Line) -> String {
        l.spans.iter().map(|s| s.content.as_ref()).collect()
    }

    fn w(s: &str, width: usize) -> Vec<String> {
        wrap(&[Span::raw(s.to_owned())], width, &[], &[]).iter().map(text).collect()
    }

    #[test]
    fn clean_drops_controls_and_bidi_and_expands_tabs() {
        assert_eq!(clean("a\x1b]52;c;QQ==\x07b\x1b[2Jc\u{202e}d\x7f\u{9b}\te\nf"), "a]52;c;QQ==b[2Jcd    e\nf");
    }

    #[test]
    fn breaks_at_spaces() {
        assert_eq!(w("the quick brown fox", 10), ["the quick", "brown fox"]);
    }

    #[test]
    fn hard_breaks_a_long_word() {
        assert_eq!(w("abcdefghij", 4), ["abcd", "efgh", "ij"]);
    }

    #[test]
    fn wide_characters_count_two_cells() {
        assert_eq!(w("日本語日本語", 6), ["日本語", "日本語"]);
    }

    #[test]
    fn hanging_indent_applies_to_continuations() {
        let first = [Span::raw("- ")];
        let rest = [Span::raw("  ")];
        let l = wrap(&[Span::raw("aaa bbb ccc")], 7, &first, &rest);
        assert_eq!(l.iter().map(text).collect::<Vec<_>>(), ["- aaa", "  bbb", "  ccc"]);
    }

    #[test]
    fn empty_input_is_one_line_and_width_zero_does_not_loop() {
        assert_eq!(w("", 10), [""]);
        assert!(!w("abc", 0).is_empty());
    }

    #[test]
    fn exact_fit_does_not_add_an_empty_line() {
        assert_eq!(w("abcd", 4), ["abcd"]);
    }
}
