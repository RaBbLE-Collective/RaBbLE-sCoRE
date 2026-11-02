//! The face in the shell: a 24x6 mini mark in the header and, on `/face`, a full face in the transcript.
//! `rabble-face` does the drawing (pure functions); this file owns the clock, the mood and the paint
//! into the terminal buffer.
//!
//! Cadence: the run loop asks `due()` for the next frame time and calls `tick` only on an iteration that
//! carried no input, so a key or a wheel notch never waits behind a face frame. A frame is computed
//! inline (the mark about 0.2 ms, the full face about 1.3 ms at 260x70, see `examples/bench.rs` in
//! rabble-face) and a paint is requested only when the cells changed.

use std::{sync::Arc, time::{Duration, Instant}};

use rabble_face::{
    degrade, driver::{BG_FPS, FPS, FULL_FPS}, Detail, Driver, Face, FacePalette, Grid, Layer, Mood, Provider,
};
use rabble_theme::Rgb;
use ratatui::{buffer::Buffer, layout::Rect, style::Style};

use crate::theme::{rgb, Theme};

/// `RABBLE_GLYPHS=safe|octant` overrides; VS Code's terminal has no octant glyphs, so it gets quadrants.
pub fn safe_glyphs() -> bool {
    let g = std::env::var("RABBLE_GLYPHS").unwrap_or_default().to_lowercase();
    g == "safe" || (g != "octant" && std::env::var("TERM_PROGRAM").as_deref() == Ok("vscode"))
}

/// One face (mini or full) with its clock, eased pose, caches and the latest cells.
pub struct FaceView {
    detail: Detail,
    pal: FacePalette,
    safe: bool,
    face: Option<Face>,
    size: (usize, usize),
    driver: Driver,
    t0: Instant,
    provider: Option<Provider>,
    base: Option<Grid>,
    base_at: f64,
    layer: Option<Arc<Layer>>,
    pub cells: Grid,
    next: Instant,
    pub frames: u64,
}

impl FaceView {
    pub fn new(pal: FacePalette, detail: Detail, safe: bool, now: Instant) -> FaceView {
        FaceView {
            detail, pal, safe, face: None, size: (0, 0), driver: Driver::new(), t0: now, provider: None, base: None, base_at: f64::NEG_INFINITY,
            layer: None, cells: Vec::new(), next: now, frames: 0,
        }
    }

    pub fn size(&self) -> (usize, usize) {
        self.size
    }

    /// Resize: the face is rebuilt on the next tick (the mesh depends on the size).
    /// Returns true when the size changed (the caller computes a frame at once so the new size never paints blank).
    pub fn fit(&mut self, cols: usize, rows: usize) -> bool {
        let changed = self.size != (cols, rows);
        if changed {
            self.size = (cols, rows);
            (self.face, self.base, self.layer) = (None, None, None);
            self.cells.clear();
            self.next = self.t0; // due at once
        }
        changed
    }

    pub fn set_mood(&mut self, mood: Mood, now: Instant) {
        self.driver.set_mood(mood, now.saturating_duration_since(self.t0).as_secs_f64());
    }

    pub fn set_provider(&mut self, p: Option<Provider>) {
        if p != self.provider {
            self.provider = p;
            self.layer = None;
            if let Some(f) = &mut self.face {
                f.set_provider(p);
            }
        }
    }

    pub fn set_signal(&mut self, capability: f64, stability: f64) {
        self.driver.set_signal(capability, stability);
    }

    pub fn due(&self) -> Instant {
        self.next
    }

    fn fps(&self) -> f64 {
        if self.detail == Detail::Full { FULL_FPS } else { FPS }
    }

    /// Compute the next frame. Returns true when the cells changed (a paint is wanted).
    pub fn tick(&mut self, now: Instant) -> bool {
        self.next = now + Duration::from_secs_f64(1.0 / self.fps());
        let (cols, rows) = self.size;
        if self.face.is_none() {
            if self.detail == Detail::Full && (cols < 4 || rows < 2) || cols == 0 || rows == 0 {
                return false;
            }
            let mut f = Face::new(cols, rows, self.pal, self.detail, self.safe);
            f.set_provider(self.provider);
            self.face = Some(f);
        }
        let t = now.saturating_duration_since(self.t0).as_secs_f64();
        let (blink, gaze) = self.driver.tick(t, t);
        let face = self.face.as_mut().unwrap();
        face.pose = Some(self.driver.pose());
        face.expr = self.driver.expr();
        face.block = self.driver.block();
        let raw = if self.detail == Detail::Full {
            // the slow layers (envelope, mesh, orb) refresh at BG_FPS under the fast eye layer
            if self.base.is_none() || t - self.base_at >= 1.0 / BG_FPS {
                self.base = Some(face.background(t));
                self.base_at = t;
            }
            let layer = face.layer(t, blink, gaze, self.layer.as_ref());
            let g = face.assemble(self.base.as_ref().unwrap(), Some(&layer));
            self.layer = Some(layer);
            g
        } else {
            face.frame(t, blink, gaze)
        };
        let cells = degrade(&raw, self.driver.breaking, &self.pal, &mut self.driver.rng, &mut self.driver.glitch, self.driver.noise);
        self.frames += 1;
        let changed = cells != self.cells;
        self.cells = cells;
        changed
    }

    /// Paint rows `skip..` of the cells into `area`. NO_COLOR gets the word, like the Python widget.
    pub fn draw(&self, area: Rect, buf: &mut Buffer, th: &Theme, skip: u16) {
        if !th.color {
            let mid = (self.size.1 / 2) as u16;
            if mid >= skip && mid - skip < area.height {
                let word = "RaBbLE";
                let x = area.x + (area.width.saturating_sub(word.len() as u16)) / 2;
                buf.set_string(x, area.y + (mid - skip), word, Style::new());
            }
            return;
        }
        let bg = self.pal.bg;
        let conv = |c: Rgb| rgb(c);
        for dy in 0..area.height {
            let Some(row) = self.cells.get((dy + skip) as usize) else { break };
            for dx in 0..area.width.min(row.len() as u16) {
                let c = row[dx as usize];
                // cells on the palette background take the terminal's own, so a translucent terminal shows through
                let bgc = if c.bg == bg { ratatui::style::Color::Reset } else { conv(c.bg) };
                let cell = &mut buf[(area.x + dx, area.y + dy)];
                cell.set_char(c.ch);
                cell.set_style(Style::new().fg(conv(c.fg)).bg(bgc));
            }
        }
    }
}

/// The header mark, the optional full face and the state that steers them.
pub struct FaceHub {
    pal: FacePalette,
    safe: bool,
    pub mini: FaceView,
    pub full: Option<FaceView>,
    pub mini_visible: bool,
    pub full_visible: bool,
    mood: Mood,
    provider: Option<Provider>,
    signal: Option<(f64, f64)>,
}

impl FaceHub {
    pub fn new(pal: FacePalette, safe: bool, now: Instant) -> FaceHub {
        let mut mini = FaceView::new(pal, Detail::Mini, safe, now);
        mini.fit(24, 6);
        FaceHub { pal, safe, mini, full: None, mini_visible: false, full_visible: false, mood: Mood::Idle, provider: None, signal: None }
    }

    pub fn mood(&self) -> Mood {
        self.mood
    }

    pub fn provider(&self) -> Option<Provider> {
        self.provider
    }

    #[cfg(test)]
    pub fn mini_provider_for_test(&self) -> Option<Provider> {
        self.mini.provider
    }

    #[cfg(test)]
    pub fn mini_noise_for_test(&self) -> f64 {
        self.mini.driver.noise
    }

    pub fn set_mood(&mut self, mood: Mood, now: Instant) {
        if mood != self.mood {
            self.mood = mood;
            self.mini.set_mood(mood, now);
            if let Some(f) = &mut self.full {
                f.set_mood(mood, now);
            }
        }
    }

    pub fn set_provider(&mut self, p: Option<Provider>) {
        if p != self.provider {
            self.provider = p;
            self.mini.set_provider(p);
            if let Some(f) = &mut self.full {
                f.set_provider(p);
            }
        }
    }

    pub fn set_signal(&mut self, capability: f64, stability: f64) {
        self.signal = Some((capability, stability));
        self.mini.set_signal(capability, stability);
        if let Some(f) = &mut self.full {
            f.set_signal(capability, stability);
        }
    }

    /// `/face`: a full face carrying the current mood, provider and signal.
    pub fn show_full(&mut self, now: Instant) {
        let mut f = FaceView::new(self.pal, Detail::Full, self.safe, now);
        f.set_provider(self.provider);
        if let Some((c, s)) = self.signal {
            f.set_signal(c, s);
        }
        f.set_mood(self.mood, now);
        self.full = Some(f);
    }

    pub fn hide_full(&mut self) {
        self.full = None;
        self.full_visible = false;
    }

    /// The next frame time over the faces that are on screen.
    pub fn due(&self) -> Option<Instant> {
        let mut d: Option<Instant> = None;
        if self.mini_visible {
            d = Some(self.mini.due());
        }
        if let (true, Some(f)) = (self.full_visible, &self.full) {
            d = Some(d.map_or(f.due(), |x| x.min(f.due())));
        }
        d
    }

    /// Tick every visible face that is due. True when any changed.
    pub fn tick(&mut self, now: Instant) -> bool {
        let mut changed = false;
        if self.mini_visible && now >= self.mini.due() {
            changed |= self.mini.tick(now);
        }
        if let (true, Some(f)) = (self.full_visible, &mut self.full) {
            if now >= f.due() {
                changed |= f.tick(now);
            }
        }
        changed
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::Glyphs;
    use rabble_theme::Palette;

    pub fn palette() -> Palette {
        Palette::parse(
            "  bg: \"#0a0010\"\n  surface: \"#101018\"\n  text: \"#e8e6f0\"\n  muted: \"#6b6880\"\n  magenta: \"#ff2bd6\"\n  cyan: \"#00f5ff\"\n  violet: \"#8a4dff\"\n  pink: \"#ff7ab8\"\n",
        )
    }

    fn hub(now: Instant) -> FaceHub {
        FaceHub::new(FacePalette::from_palette(&palette()), false, now)
    }

    #[test]
    fn octant_glyphs_are_one_cell_wide() {
        use unicode_width::UnicodeWidthChar;
        for cp in 0x1CD00u32..0x1CDE6 {
            assert_eq!(char::from_u32(cp).unwrap().width(), Some(1), "{cp:#x}");
        }
        for c in ['▀', '▘', '▟', '⣿'] {
            assert_eq!(c.width(), Some(1));
        }
    }

    #[test]
    fn the_mark_ticks_at_its_own_cadence_and_only_when_visible() {
        let t0 = Instant::now();
        let mut h = hub(t0);
        assert_eq!(h.due(), None, "nothing visible, nothing due");
        h.mini_visible = true;
        assert!(h.tick(t0), "the first frame always differs from nothing");
        assert_eq!(h.mini.cells.len(), 6);
        assert!(h.due().unwrap() > t0 && h.due().unwrap() <= t0 + Duration::from_millis(51));
        let frames = h.mini.frames;
        assert!(!h.tick(t0 + Duration::from_millis(1)), "not due yet: no work");
        assert_eq!(h.mini.frames, frames);
        h.tick(t0 + Duration::from_millis(60));
        assert_eq!(h.mini.frames, frames + 1);
    }

    #[test]
    fn mood_and_provider_reach_both_faces() {
        let t0 = Instant::now();
        let mut h = hub(t0);
        h.show_full(t0);
        h.set_mood(Mood::Error, t0);
        h.set_provider(Some(Provider::Google));
        h.set_signal(40.0, 95.0);
        assert_eq!(h.mood(), Mood::Error);
        assert_eq!(h.mini.driver.target_expr().name(), "sad");
        assert_eq!(h.full.as_ref().unwrap().driver.target_expr().name(), "sad");
        assert_eq!(h.full.as_ref().unwrap().provider, Some(Provider::Google));
        assert!(h.mini.driver.noise > 0.0);
    }

    #[test]
    fn the_full_face_draws_into_the_buffer_and_survives_a_resize() {
        let t0 = Instant::now();
        let th = Theme::new(Some(&palette()), false, Glyphs::FANCY);
        let mut h = hub(t0);
        h.show_full(t0);
        h.full_visible = true;
        let f = h.full.as_mut().unwrap();
        f.fit(80, 24);
        assert!(f.tick(t0));
        let mut buf = Buffer::empty(Rect::new(0, 0, 80, 24));
        f.draw(Rect::new(0, 0, 80, 24), &mut buf, &th, 0);
        assert!((0..80).any(|x| (0..24).any(|y| buf[(x, y)].symbol() != " ")));
        f.fit(60, 14);
        assert!(f.cells.is_empty(), "stale cells are dropped on resize");
        assert!(f.tick(t0 + Duration::from_millis(40)));
        assert_eq!((f.cells.len(), f.cells[0].len()), (14, 60));
        f.fit(3, 1);
        assert!(!f.tick(t0 + Duration::from_millis(80)), "too small to draw: no face, no panic");
    }

    #[test]
    fn no_color_shows_the_word_and_nothing_else() {
        let t0 = Instant::now();
        let th = Theme::new(Some(&palette()), true, Glyphs::SAFE);
        let mut h = hub(t0);
        h.mini_visible = true;
        h.tick(t0);
        let mut buf = Buffer::empty(Rect::new(0, 0, 24, 6));
        h.mini.draw(Rect::new(0, 0, 24, 6), &mut buf, &th, 0);
        let text: String = (0..6).flat_map(|y| (0..24).map(move |x| (x, y))).map(|(x, y)| buf[(x, y)].symbol().to_owned()).collect();
        assert_eq!(text.trim(), "RaBbLE");
        assert!((0..6).all(|y| (0..24).all(|x| buf[(x, y)].fg == ratatui::style::Color::Reset)));
    }

    #[test]
    fn palette_background_cells_use_the_terminals_own() {
        let t0 = Instant::now();
        let th = Theme::new(Some(&palette()), false, Glyphs::FANCY);
        let mut h = hub(t0);
        h.mini_visible = true;
        h.tick(t0);
        let mut buf = Buffer::empty(Rect::new(0, 0, 24, 6));
        h.mini.draw(Rect::new(0, 0, 24, 6), &mut buf, &th, 0);
        assert_eq!(buf[(0, 0)].bg, ratatui::style::Color::Reset, "a corner of the mark is the void: terminal default");
    }

    #[test]
    fn safe_glyph_override_reads_the_environment_names() {
        // the pure part: the mark built with `safe` never emits octant glyphs
        let t0 = Instant::now();
        let mut h = FaceHub::new(FacePalette::from_palette(&palette()), true, t0);
        h.mini_visible = true;
        h.tick(t0);
        assert!(h.mini.cells.iter().flatten().all(|c| c.ch == ' ' || ('\u{2580}'..='\u{259F}').contains(&c.ch) || ('\u{2800}'..='\u{28FF}').contains(&c.ch)));
    }
}
