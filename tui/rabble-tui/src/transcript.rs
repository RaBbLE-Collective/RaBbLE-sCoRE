//! Synthetic transcript for the R0 spike: styled lines, rendered at the rounded glide position.

use ratatui::{
    buffer::Buffer,
    layout::Rect,
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::Widget,
};

pub struct Transcript {
    pub lines: Vec<Line<'static>>,
}

impl Transcript {
    pub fn synthetic(n: usize) -> Self {
        let lines = (0..n)
            .map(|i| match i % 12 {
                0 => Line::from(Span::styled(
                    format!("## Section {:05}", i / 12),
                    Style::new().fg(Color::Rgb(0, 245, 255)).add_modifier(Modifier::BOLD),
                )),
                5 => Line::from(vec![
                    Span::styled(format!("{i:05} "), Style::new().fg(Color::Rgb(107, 104, 128))),
                    Span::styled("tool ", Style::new().fg(Color::Rgb(255, 0, 170))),
                    Span::raw("result [with brackets] and `code` runs, wrapped at the edge of the pane"),
                ]),
                _ => Line::from(vec![
                    Span::styled(format!("{i:05} "), Style::new().fg(Color::Rgb(107, 104, 128))),
                    Span::styled("the quick brown fox ", Style::new().fg(Color::Rgb(232, 230, 240))),
                    Span::styled("jumps over ", Style::new().fg(Color::Rgb(0, 245, 255))),
                    Span::styled("the lazy dog", Style::new().fg(Color::Rgb(255, 0, 170))),
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
