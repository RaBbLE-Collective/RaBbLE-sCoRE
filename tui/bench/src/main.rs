//! Headless frame-cost bench: render the synthetic transcript into a ratatui Buffer
//! with a diff against the previous frame (what the backend would write) and report p50/p99.
//! Target T2: transcript frame <= 4 ms.

use rabble_tui::{glide::Glide, transcript::{Transcript, View}};
use ratatui::{buffer::Buffer, layout::Rect, widgets::Widget};
use std::time::Instant;

fn pct(v: &mut [f64], p: f64) -> f64 {
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    v[((v.len() - 1) as f64 * p) as usize]
}

fn main() {
    let t = Transcript::synthetic(10_000);
    for (w, h) in [(120u16, 40u16), (200, 55), (260, 70)] {
        let area = Rect::new(0, 0, w, h);
        let max = t.max_scroll(h);
        let mut g = Glide::default();
        let (mut prev, mut cur) = (Buffer::empty(area), Buffer::empty(area));
        let (mut render, mut diff) = (Vec::new(), Vec::new());
        for i in 0..2400 {
            if i % 24 == 0 { for _ in 0..4 { g.wheel(1.0, max) } }
            g.tick(1.0 / 120.0, max);
            let s = Instant::now();
            cur.reset();
            View { t: &t, pos: g.pos }.render(area, &mut cur);
            render.push(s.elapsed().as_secs_f64() * 1e3);
            let s = Instant::now();
            let n = prev.diff(&cur).len();
            std::hint::black_box(n);
            diff.push(s.elapsed().as_secs_f64() * 1e3);
            std::mem::swap(&mut prev, &mut cur);
        }
        println!("{w}x{h}: render p50 {:.3} p99 {:.3} ms | diff p50 {:.3} p99 {:.3} ms",
            pct(&mut render.clone(), 0.5), pct(&mut render, 0.99), pct(&mut diff.clone(), 0.5), pct(&mut diff, 0.99));
    }
}
