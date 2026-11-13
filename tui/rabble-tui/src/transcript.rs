//! Synthetic transcript for the R0 spike: styled lines, rendered at the rounded glide position.

use ratatui::{
    buffer::Buffer,
    layout::Rect,
    text::{Line, Span},
    widgets::Widget,
};

use crate::theme::Theme;

pub struct Transcript {
    pub lines: Vec<Line<'static>>,
}

impl Transcript {
    pub fn synthetic(n: usize) -> Self {
        Self::synthetic_with(n, &Theme::from_env())
    }

    pub fn synthetic_with(n: usize, th: &Theme) -> Self {
        let lines = (0..n)
            .map(|i| match i % 12 {
                0 => Line::from(Span::styled(format!("## Section {:05}", i / 12), th.heading)),
                5 => Line::from(vec![
                    Span::styled(format!("{i:05} "), th.muted),
                    Span::styled("tool ", th.accent),
                    Span::raw("result [with brackets] and `code` runs, wrapped at the edge of the pane"),
                ]),
                _ => Line::from(vec![
                    Span::styled(format!("{i:05} "), th.muted),
                    Span::styled("the quick brown fox ", th.text),
                    Span::styled("jumps over ", th.secondary),
                    Span::styled("the lazy dog", th.accent),
                ]),
            })
            .collect();
        Self { lines }
    }

    pub fn len(&self) -> usize {
        self.lines.len()
    }
    pub fn is_empty(&self) -> bool {
        self.lines.is_empty()
    }
    pub fn max_scroll(&self, height: u16) -> f64 {
        self.lines.len().saturating_sub(height as usize) as f64
    }
}

pub struct View<'a> {
    pub t: &'a Transcript,
    pub pos: f64,
}

impl Widget for View<'_> {
    fn render(self, area: Rect, buf: &mut Buffer) {
        let top = self.pos.round().max(0.0) as usize;
        for (row, line) in self.t.lines.iter().skip(top).take(area.height as usize).enumerate() {
            buf.set_line(area.x, area.y + row as u16, line, area.width);
        }
    }
}
