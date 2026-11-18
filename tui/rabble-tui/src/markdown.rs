//! A small Markdown renderer for assistant replies: headings, paragraphs (soft breaks joined),
//! bullet and numbered lists with hanging indent, block quotes, fenced code, rules, pipe tables
//! (kept verbatim), and inline `code`, **bold**, *italic*, [text](url) (shown as text).

use ratatui::{style::{Modifier, Style}, text::{Line, Span}};

use crate::{theme::Theme, wrap::{self, str_width}};

/// Inline styling of one run of text.
pub fn inline(s: &str, base: Style, th: &Theme) -> Vec<Span<'static>> {
    let cs: Vec<char> = s.chars().collect();
    let mut out: Vec<Span<'static>> = Vec::new();
    let mut buf = String::new();
    let flush = |buf: &mut String, st: Style, out: &mut Vec<Span<'static>>| {
        if !buf.is_empty() {
            out.push(Span::styled(std::mem::take(buf), st));
        }
    };
    let find = |from: usize, pat: &[char]| (from..cs.len().saturating_sub(pat.len() - 1)).find(|&j| cs[j..j + pat.len()] == *pat);
    let mut i = 0;
    while i < cs.len() {
        let c = cs[i];
        if c == '\\' && i + 1 < cs.len() && "`*_[]\\".contains(cs[i + 1]) {
            buf.push(cs[i + 1]);
            i += 2;
        } else if c == '`' {
            if let Some(j) = find(i + 1, &['`']) {
                flush(&mut buf, base, &mut out);
                out.push(Span::styled(cs[i + 1..j].iter().collect::<String>(), th.code));
                i = j + 1;
                continue;
            }
            buf.push(c);
            i += 1;
        } else if c == '*' && cs.get(i + 1) == Some(&'*') {
            if let Some(j) = find(i + 2, &['*', '*']).filter(|&j| j > i + 2) {
                flush(&mut buf, base, &mut out);
                out.extend(inline(&cs[i + 2..j].iter().collect::<String>(), base.add_modifier(Modifier::BOLD), th));
                i = j + 2;
                continue;
            }
            buf.push(c);
            i += 1;
        } else if (c == '*' || c == '_') && cs.get(i + 1).is_some_and(|n| !n.is_whitespace() && *n != c)
            && (i == 0 || !cs[i - 1].is_alphanumeric())
        {
            if let Some(j) = find(i + 1, &[c]).filter(|&j| j > i + 1 && !cs[j - 1].is_whitespace() && cs.get(j + 1).is_none_or(|n| !n.is_alphanumeric())) {
                flush(&mut buf, base, &mut out);
                out.extend(inline(&cs[i + 1..j].iter().collect::<String>(), base.add_modifier(Modifier::ITALIC), th));
                i = j + 1;
                continue;
            }
            buf.push(c);
            i += 1;
        } else if c == '[' {
            // [text](url): show the text
            if let Some(j) = find(i + 1, &[']']).filter(|&j| cs.get(j + 1) == Some(&'(')) {
                if let Some(k) = find(j + 2, &[')']) {
                    flush(&mut buf, base, &mut out);
                    out.extend(inline(&cs[i + 1..j].iter().collect::<String>(), base.add_modifier(Modifier::UNDERLINED), th));
                    i = k + 1;
                    continue;
                }
            }
            buf.push(c);
            i += 1;
        } else {
            buf.push(c);
            i += 1;
        }
    }
    flush(&mut buf, base, &mut out);
    out
}

fn list_marker(line: &str) -> Option<(usize, String, &str)> {
    let indent = line.len() - line.trim_start().len();
    let t = line.trim_start();
    for b in ["- ", "* ", "+ "] {
        if let Some(r) = t.strip_prefix(b) {
            return Some((indent, "•".into(), r));
        }
    }
    let digits = t.chars().take_while(char::is_ascii_digit).count();
    if (1..=3).contains(&digits) {
        if let Some(r) = t[digits..].strip_prefix(". ").or_else(|| t[digits..].strip_prefix(") ")) {
            return Some((indent, format!("{}.", &t[..digits]), r));
        }
    }
    None
}

pub fn render(text: &str, th: &Theme, width: usize) -> Vec<Line<'static>> {
    let width = width.max(4);
    let mut out: Vec<Line<'static>> = Vec::new();
    let mut para: Vec<Span<'static>> = Vec::new();
    let mut in_code = false;
    let bullet = |m: &str| if m == "•" { th.glyphs.bullet.to_owned() } else { m.to_owned() };

    let flush = |para: &mut Vec<Span<'static>>, out: &mut Vec<Line<'static>>| {
        if !para.is_empty() {
            out.extend(wrap::wrap(para, width, &[], &[]));
            para.clear();
        }
    };
    for raw in text.split('\n') {
        let raw = raw.trim_end_matches('\r');
        let t = raw.trim_start();
        if t.starts_with("```") || t.starts_with("~~~") {
            flush(&mut para, &mut out);
            in_code = !in_code;
            continue;
        }
        if in_code {
            // code is never re-flowed: hard-wrap only
            let cells = [Span::styled(raw.to_owned(), th.code)];
            // the indent is HIDDEN (blank either way) so a copy can tell it from the code's own spaces
            let ind = [Span::styled("  ", Style::new().add_modifier(Modifier::HIDDEN))];
            out.extend(wrap::wrap(&cells, width.saturating_sub(2), &ind, &ind));
            continue;
        }
        if t.is_empty() {
            flush(&mut para, &mut out);
            if out.last().is_some_and(|l| !l.spans.is_empty()) {
                out.push(Line::default());
            }
            continue;
        }
        if let Some(h) = t.strip_prefix('#').map(|r| r.trim_start_matches('#')).filter(|r| r.starts_with(' ')) {
            flush(&mut para, &mut out);
            out.extend(wrap::wrap(&inline(h.trim(), th.heading, th), width, &[], &[]));
            continue;
        }
        if t.len() >= 3 && t.chars().all(|c| matches!(c, '-' | '*' | '_' | ' ')) && t.chars().filter(|c| !c.is_whitespace()).count() >= 3 && t.chars().filter(|c| !c.is_whitespace()).all(|c| c == t.chars().next().unwrap()) {
            flush(&mut para, &mut out);
            out.push(Line::from(Span::styled(th.glyphs.rule.to_string().repeat(width.min(40)), th.muted)));
            continue;
        }
        if let Some(q) = t.strip_prefix('>') {
            flush(&mut para, &mut out);
            let bar = vec![Span::styled(format!("{} ", th.glyphs.quote), th.muted)];
            out.extend(wrap::wrap(&inline(q.trim_start(), th.muted, th), width, &bar, &bar));
            continue;
        }
        if t.starts_with('|') {
            flush(&mut para, &mut out);
            out.extend(wrap::wrap(&[Span::styled(raw.to_owned(), th.text)], width, &[], &[Span::raw("  ")]));
            continue;
        }
        if let Some((ind, m, rest)) = list_marker(raw) {
            flush(&mut para, &mut out);
            let pad = " ".repeat(ind.min(8));
            let first = vec![Span::raw(format!("{pad}{} ", bullet(&m)))];
            let hang = vec![Span::raw(" ".repeat(str_width(&pad) + str_width(&bullet(&m)) + 1))];
            out.extend(wrap::wrap(&inline(rest, th.text, th), width, &first, &hang));
            continue;
        }
        if !para.is_empty() {
            para.push(Span::raw(" "));
        }
        para.extend(inline(t, th.text, th));
    }
    flush(&mut para, &mut out);
    while out.last().is_some_and(|l| l.spans.is_empty()) {
        out.pop();
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::Glyphs;

    fn th() -> Theme {
        Theme::new(None, true, Glyphs::SAFE)
    }
    fn txt(l: &Line) -> String {
        l.spans.iter().map(|s| s.content.as_ref()).collect()
    }
    fn lines(s: &str, w: usize) -> Vec<String> {
        render(s, &th(), w).iter().map(txt).collect()
    }

    #[test]
    fn paragraphs_join_soft_breaks_and_blank_lines_separate() {
        assert_eq!(lines("one\ntwo\n\nthree", 40), ["one two", "", "three"]);
    }

    #[test]
    fn lists_hang() {
        assert_eq!(lines("- alpha beta gamma\n1. one", 10), ["* alpha", "  beta", "  gamma", "1. one"]);
    }

    #[test]
    fn code_fence_is_verbatim_and_not_markdown() {
        assert_eq!(lines("```rust\nlet *x* = `y`;\n```", 40), ["  let *x* = `y`;"]);
    }

    #[test]
    fn inline_code_bold_italic_and_links() {
        let l = render("a `b` **c** *d* [e](http://x)", &th(), 40);
        assert_eq!(txt(&l[0]), "a b c d e");
        assert!(l[0].spans.iter().any(|s| s.content == "c" && s.style.add_modifier.contains(Modifier::BOLD)));
        assert!(l[0].spans.iter().any(|s| s.content == "d" && s.style.add_modifier.contains(Modifier::ITALIC)));
    }

    #[test]
    fn underscores_inside_words_are_left_alone() {
        assert_eq!(lines("snake_case_name and a*b", 40), ["snake_case_name and a*b"]);
    }

    #[test]
    fn heading_quote_rule() {
        let l = lines("## Title\n> quoted\n---", 40);
        assert_eq!(l[0], "Title");
        assert_eq!(l[1], "| quoted");
        assert!(l[2].starts_with("---"));
    }

    #[test]
    fn brackets_survive_unmatched() {
        assert_eq!(lines("list [Region(x=1), Region(y=2)] [ and ]", 60), ["list [Region(x=1), Region(y=2)] [ and ]"]);
    }
}
