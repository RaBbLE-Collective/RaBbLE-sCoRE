//! Header (two or three rows of text, or the six Python info lines beside the mark) and the divider under it. With the face mark
//! showing, the header is `FACE_ROWS` (full) or `FACE_COMPACT_ROWS` (micro) tall: the left columns belong to the mark and the
//! text lines sit beside it.

use ratatui::{buffer::Buffer, layout::Rect, text::{Line, Span}};

use crate::{theme::Theme, usage::{self, Usage}, wrap::{clean, spans_width, str_width}};

#[derive(Debug, Default, Clone)]
pub struct HeaderState {
    pub model: String,
    pub mode: String,
    pub trust: String,
    pub crew: bool,
    pub cwd: String,
    pub entropy: Option<(f64, String)>,
    pub cost: Option<f64>,
    pub context_pct: Option<f64>,
    pub limit: Option<String>,
    pub session: String,
    pub connected: bool,
    pub capability: Option<i64>,
    pub categories: Vec<(String, i64)>,
    pub stability: Option<i64>,
    pub local: bool,
    pub usage: Option<Usage>,
}

fn home_short(cwd: &str) -> String {
    match std::env::var("HOME") {
        Ok(h) if !h.is_empty() && cwd.starts_with(&h) => format!("~{}", &cwd[h.len()..]),
        _ => cwd.to_owned(),
    }
}

/// The wordmark, one span per character on the same cyan/magenta flow as the divider (`phase` drifts it);
/// every header tier draws this. Without colour it is the accent, bold.
fn wordmark(th: &Theme, phase: f64) -> Vec<Span<'static>> {
    let bold = ratatui::style::Modifier::BOLD;
    "RaBbLE".chars().enumerate()
        .map(|(i, ch)| Span::styled(ch.to_string(), th.flow(i, 6, phase, th.accent).add_modifier(bold)))
        .collect()
}

impl HeaderState {
    fn bar(&self, th: &Theme, pct: f64, band: u8, width: usize) -> Vec<Span<'static>> {
        let filled = ((pct / 100.0 * width as f64).round().max(0.0) as usize).min(width);
        let st = match band { 0 => th.secondary, 1 => th.warn, _ => th.error };
        vec![
            Span::styled(th.glyphs.bar[0].to_string().repeat(filled), st),
            Span::styled(th.glyphs.bar[1].to_string().repeat(width - filled), th.muted),
        ]
    }

    /// `usage 5h ▓▓░░ 38% ↺2h  wk ...`, or why there is none: the rich header's fifth line, and the compact one's third.
    fn usage_spans(&self, th: &Theme, now: f64) -> Vec<Span<'static>> {
        let mut l = vec![Span::styled("usage ", th.muted)];
        match self.usage.filter(|u| !u.is_empty()) {
            None => l.push(Span::styled("-- (OS tracker not reporting)", th.muted)),
            Some(u) => {
                for (label, w) in [("5h", u.five_hour), ("wk", u.week)] {
                    let Some(w) = w else { continue };
                    l.push(Span::styled(format!("{label} "), th.muted));
                    l.extend(self.bar(th, w.pct, w.band(), 8));
                    l.push(Span::raw(format!(" {:.0}%", w.pct)));
                    l.push(Span::styled(format!(" ↺{}  ", usage::fmt_remaining(w.resets_in(now))), th.muted));
                }
                if u.stale() {
                    l.push(Span::styled(format!("stale {}", usage::fmt_remaining(u.age)), th.warn));
                }
            }
        }
        l
    }

    /// The six lines Python's header shows beside the mark: wordmark and cwd, model/mode/tier/crew,
    /// capability bars, entropy and context, provider usage, limit and cost.
    pub fn rich_lines(&self, th: &Theme, width: usize, now: f64, phase: f64) -> Vec<Line<'static>> {
        let bold = ratatui::style::Modifier::BOLD;
        let mut l1 = wordmark(th, phase);
        l1.push(Span::styled(format!("  {}", clean(&home_short(&self.cwd))), th.muted));
        if !self.connected {
            l1.push(Span::styled("  offline", th.error));
        }
        let tier = format!("{}{}{}", if self.trust.is_empty() { "standard" } else { &self.trust }, if self.local { " local" } else { "" },
            self.capability.map(|c| format!(" {c}")).unwrap_or_default());
        let model = if self.model.is_empty() { "default".to_owned() } else { clean(&self.model) };
        let l2 = vec![Span::styled(format!("{model} · mode {} · {tier} · crew {}", self.mode, if self.crew { "on" } else { "off" }), th.text.add_modifier(bold))];

        let mut l3 = vec![Span::styled("capability ", th.muted)];
        if self.categories.is_empty() {
            l3.push(Span::raw("--"));
        }
        for (name, short) in [("general", "gen"), ("coding", "code"), ("tools", "tools"), ("recall", "recall")] {
            if let Some((_, v)) = self.categories.iter().find(|(k, _)| k == name) {
                l3.push(Span::styled(format!("{short} "), th.muted));
                l3.extend(self.bar(th, *v as f64, if *v >= 60 { 0 } else if *v >= 40 { 1 } else { 2 }, 6));
                l3.push(Span::raw(format!(" {v}  ")));
            }
        }

        let mut l4 = vec![];
        if let Some((score, band)) = &self.entropy {
            let (glyph, st) = match band.as_str() {
                "ELEVATED" => ("▲", th.warn),
                "DEGRADED" => ("⚠", th.accent),
                "UNSTABLE" => ("✗", th.error),
                _ => ("◆", th.secondary),
            };
            let glyph = if th.glyphs.vert == '|' { "*" } else { glyph };
            l4.push(Span::styled(format!("{glyph} {}", band.to_lowercase()), st));
            l4.push(Span::styled(format!(" entropy {score:.2}"), th.muted));
            if let Some(s) = self.stability {
                l4.push(Span::raw(format!(" · stab {s}")));
            }
            l4.push(Span::raw(" · "));
        }
        match self.context_pct {
            None => l4.push(Span::raw("ctx --")),
            Some(p) => {
                l4.push(Span::raw(format!("ctx {p:.0}% ")));
                l4.extend(self.bar(th, p, if p >= 80.0 { 2 } else if p >= 60.0 { 1 } else { 0 }, 8));
            }
        }

        let l5 = self.usage_spans(th, now);

        let mut l6 = vec![match self.limit.as_deref() {
            None => Span::styled("limits ok", th.muted),
            Some(l) => Span::styled(format!("limit {l}"), if l == "warning" { th.warn } else { th.error }),
        }];
        l6.push(Span::styled(format!(" · {} API equiv", self.cost.map_or("--".to_owned(), |c| format!("${c:.4}"))), th.muted));

        [l1, l2, l3, l4, l5, l6].into_iter().map(|mut v| {
            while spans_width(&v) > width && v.len() > 1 {
                v.pop();
            }
            Line::from(v)
        }).collect()
    }

    /// The compact tier's text: the model line, the cwd line, and (when the header has the room) the usage line.
    pub fn lines(&self, th: &Theme, width: usize, phase: f64) -> [Line<'static>; 3] {
        let sep = || Span::styled(format!(" {} ", th.glyphs.bullet), th.muted);
        let mut a = wordmark(th, phase);
        a.push(sep());
        a.push(Span::styled(if self.model.is_empty() { "default".to_owned() } else { clean(&self.model) }, th.secondary));
        a.push(sep());
        a.push(Span::styled(self.mode.clone(), th.tertiary));
        if !self.trust.is_empty() {
            a.push(sep());
            a.push(Span::styled(format!("trust {}", self.trust), th.muted));
        }
        a.push(sep());
        a.push(Span::styled(if self.crew { "crew on" } else { "solo" }, th.muted));
        if !self.connected {
            a.push(sep());
            a.push(Span::styled("offline", th.error));
        }
        let mut b = vec![Span::styled(clean(&home_short(&self.cwd)), th.muted)];
        if let Some((score, band)) = &self.entropy {
            b.push(sep());
            let st = if matches!(band.as_str(), "DEGRADED" | "UNSTABLE") { th.error } else { th.ok };
            b.push(Span::styled(format!("{} {score:.2}", band.to_lowercase()), st));
        }
        if let Some(c) = self.cost {
            b.push(sep());
            b.push(Span::styled(format!("${c:.2}"), th.muted));
        }
        if let Some(p) = self.context_pct {
            b.push(sep());
            b.push(Span::styled(format!("ctx {p:.0}%"), if p >= 80.0 { th.warn } else { th.muted }));
        }
        if let Some(l) = &self.limit {
            b.push(sep());
            b.push(Span::styled(format!("limit {l}"), th.warn));
        }
        let clipw = |mut v: Vec<Span<'static>>| {
            while spans_width(&v) > width && v.len() > 1 {
                v.pop();
            }
            Line::from(v)
        };
        let c = self.usage_spans(th, usage::now());
        [clipw(a), clipw(b), clipw(c)]
    }

    /// `face_slot`: leave the left `face_slot` columns (and a one-column gap) to the face mark (0 = no mark);
    /// `rich`: the full mark's six info lines, else the two-line header.
    pub fn draw(&self, area: Rect, buf: &mut Buffer, th: &Theme, face_slot: u16, rich: bool, phase: f64) {
        let slot = if face_slot > 0 { (face_slot + 1).min(area.width) } else { 0 };
        let (x0, w) = (area.x + slot, area.width.saturating_sub(slot));
        let lines: Vec<Line<'static>> = if rich { self.rich_lines(th, w as usize, usage::now(), phase) } else { self.lines(th, w as usize, phase).into() };
        for (i, l) in lines.iter().enumerate().take(area.height as usize) {
            buf.set_line(x0, area.y + i as u16, l, w);
        }
    }
}

/// A rule with a label at the right (`working`, `awaiting approval`, ...).
/// The rule flows cyan -> magenta -> cyan (`phase` drifts it), like Python's `Divider`.
pub fn draw_divider(area: Rect, buf: &mut Buffer, th: &Theme, label: &str, phase: f64) {
    let rule = th.glyphs.rule.to_string();
    let lw = str_width(label);
    let w = area.width as usize;
    let n = w.saturating_sub(if lw > 0 { lw + 3 } else { 0 });
    let cell = |x: usize| Span::styled(rule.clone(), th.flow(x, w, phase, th.border));
    let mut spans: Vec<Span<'static>> = (0..n).map(cell).collect();
    if lw > 0 {
        spans.push(Span::styled(format!(" {label} "), th.accent));
        spans.push(cell(w - 1));
    }
    buf.set_line(area.x, area.y, &Line::from(spans), area.width);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::Glyphs;

    #[test]
    fn a_narrower_slot_gives_the_lines_the_rest_and_the_wordmark_flows_at_every_size() {
        let pal = rabble_theme::Palette::parse("  bg: \"#0a0010\"\n  surface: \"#101018\"\n  text: \"#e8e6f0\"\n  muted: \"#6b6880\"\n  magenta: \"#ff2bd6\"\n  cyan: \"#00f5ff\"\n  violet: \"#8a4dff\"\n  pink: \"#ff7ab8\"\n");
        let th = Theme::new(Some(&pal), false, Glyphs::FANCY);
        let h = HeaderState { model: "opus".into(), mode: "default".into(), trust: "strict".into(), cwd: "/x".into(), connected: true, ..Default::default() };
        let area = Rect::new(0, 0, 40, 6);
        let colours = |slot: u16, rich: bool, phase: f64| {
            let mut buf = Buffer::empty(area);
            h.draw(Rect { height: 3, ..area }, &mut buf, &th, slot, rich, phase);
            let x0 = if slot > 0 { slot + 1 } else { 0 };
            let row: String = (0..40).map(|x| buf[(x, 0)].symbol().to_owned()).collect();
            assert_eq!(&row[x0 as usize..x0 as usize + 6], "RaBbLE", "{slot}/{rich}: {row:?}");
            ((0..6).map(|i| buf[(x0 + i, 0)].fg).collect::<Vec<_>>(), row)
        };
        for phase in [0.0, 0.3] {
            let (full, _) = colours(24, true, phase);
            let (compact, crow) = colours(8, false, phase);
            let (plain, prow) = colours(0, false, phase);
            assert_eq!(compact, full, "compact wordmark colours match the full tier at phase {phase}");
            assert_eq!(plain, full, "plain wordmark colours match the full tier at phase {phase}");
            assert_ne!(full[0], full[5], "the blend runs along the letters");
            assert!(crow.starts_with("         RaBbLE") && prow.starts_with("RaBbLE"));
            assert!(crow.contains("opus") && crow.len() == prow.len());
        }
        assert_ne!(colours(0, false, 0.0).0, colours(0, false, 0.3).0, "the flow moves with the phase");
    }

    #[test]
    fn narrow_headers_drop_trailing_fields_not_wrap() {
        let th = Theme::new(None, true, Glyphs::SAFE);
        let h = HeaderState { model: "opus".into(), mode: "default".into(), trust: "strict".into(), cwd: "/x".into(), context_pct: Some(91.0), cost: Some(1.5), connected: true, ..Default::default() };
        let wide: String = h.lines(&th, 100, 0.0)[1].spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(wide.contains("ctx 91%") && wide.contains("$1.50"));
        let narrow = h.lines(&th, 8, 0.0);
        assert!(spans_width(&narrow[0].spans) <= 8 || narrow[0].spans.len() == 1);
    }

    #[test]
    fn offline_is_flagged() {
        let th = Theme::new(None, true, Glyphs::SAFE);
        let h = HeaderState::default();
        let a: String = h.lines(&th, 100, 0.0)[0].spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(a.contains("offline"));
    }
}
