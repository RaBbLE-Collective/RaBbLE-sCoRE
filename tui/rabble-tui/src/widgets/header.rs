//! Header (two rows of text) and the divider under it. With the face mark showing, the header is
//! `FACE_ROWS` tall: the left `FACE_SLOT_WIDTH` columns belong to the mark and the two text lines sit
//! beside it, vertically centred.

use ratatui::{buffer::Buffer, layout::Rect, text::{Line, Span}};

use super::FACE_SLOT_WIDTH;
use crate::{theme::Theme, wrap::{clean, spans_width, str_width}};

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
}

fn home_short(cwd: &str) -> String {
    match std::env::var("HOME") {
        Ok(h) if !h.is_empty() && cwd.starts_with(&h) => format!("~{}", &cwd[h.len()..]),
        _ => cwd.to_owned(),
    }
}

impl HeaderState {
    pub fn lines(&self, th: &Theme, width: usize) -> [Line<'static>; 2] {
        let sep = || Span::styled(format!(" {} ", th.glyphs.bullet), th.muted);
        let mut a = vec![Span::styled("RaBbLE", th.accent.add_modifier(ratatui::style::Modifier::BOLD))];
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
        [clipw(a), clipw(b)]
    }

    /// `face_slot`: leave the left `FACE_SLOT_WIDTH` columns (and a one-column gap) to the face mark and
    /// centre the two lines against it.
    pub fn draw(&self, area: Rect, buf: &mut Buffer, th: &Theme, face_slot: bool) {
        let slot = if face_slot { (FACE_SLOT_WIDTH + 1).min(area.width) } else { 0 };
        let (x0, w) = (area.x + slot, area.width.saturating_sub(slot));
        let y0 = area.y + if face_slot { area.height.saturating_sub(2) / 2 } else { 0 };
        for (i, l) in self.lines(th, w as usize).iter().enumerate().take(area.height as usize) {
            buf.set_line(x0, y0 + i as u16, l, w);
        }
    }
}

/// A rule with a label at the right (`working`, `awaiting approval`, ...).
pub fn draw_divider(area: Rect, buf: &mut Buffer, th: &Theme, label: &str) {
    let rule = th.glyphs.rule.to_string();
    let lw = str_width(label);
    let n = (area.width as usize).saturating_sub(if lw > 0 { lw + 3 } else { 0 });
    let mut spans = vec![Span::styled(rule.repeat(n), th.border)];
    if lw > 0 {
        spans.push(Span::styled(format!(" {label} "), th.accent));
        spans.push(Span::styled(rule, th.border));
    }
    buf.set_line(area.x, area.y, &Line::from(spans), area.width);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::Glyphs;

    #[test]
    fn narrow_headers_drop_trailing_fields_not_wrap() {
        let th = Theme::new(None, true, Glyphs::SAFE);
        let h = HeaderState { model: "opus".into(), mode: "default".into(), trust: "strict".into(), cwd: "/x".into(), context_pct: Some(91.0), cost: Some(1.5), connected: true, ..Default::default() };
        let wide: String = h.lines(&th, 100)[1].spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(wide.contains("ctx 91%") && wide.contains("$1.50"));
        let narrow = h.lines(&th, 8);
        assert!(spans_width(&narrow[0].spans) <= 8 || narrow[0].spans.len() == 1);
    }

    #[test]
    fn offline_is_flagged() {
        let th = Theme::new(None, true, Glyphs::SAFE);
        let h = HeaderState::default();
        let a: String = h.lines(&th, 100)[0].spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(a.contains("offline"));
    }
}
