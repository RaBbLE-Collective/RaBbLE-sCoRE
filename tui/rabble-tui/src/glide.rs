//! Wheel glide: notches move a target, a frame clock closes a fraction of the gap
//! each frame with a minimum speed so the exponential tail does not crawl.
//! Constants are the S292 Python values (`ui/widgets/transcript.py`).

pub const WHEEL_LINES: f64 = 3.0;
pub const GLIDE_RATE: f64 = 16.0; // 1/s, time constant ~60 ms
pub const GLIDE_FPS: u32 = 120;
pub const GLIDE_MIN_SPEED: f64 = 45.0; // lines/s
const LAND: f64 = 0.5;

#[derive(Debug, Clone)]
pub struct Glide {
    pub pos: f64,
    target: Option<f64>,
    /// tunables, live-adjustable in the spike; `instant` = Vim feel (a notch lands this frame)
    pub lines: f64,
    pub rate: f64,
    pub min_speed: f64,
    pub instant: bool,
}

impl Default for Glide {
    fn default() -> Self {
        Self { pos: 0.0, target: None, lines: WHEEL_LINES, rate: GLIDE_RATE, min_speed: GLIDE_MIN_SPEED, instant: false }
    }
}

impl Glide {
    /// Presets for A/B: 1 = instant line steps (Vim, cat), 2 = snappy short glide, 3 = the S292 glide.
    pub fn preset(&mut self, n: u8) {
        let (lines, rate, min_speed, instant) = match n {
            1 => (3.0, GLIDE_RATE, GLIDE_MIN_SPEED, true),
            2 => (3.0, 45.0, 140.0, false),
            _ => (WHEEL_LINES, GLIDE_RATE, GLIDE_MIN_SPEED, false),
        };
        (self.lines, self.rate, self.min_speed, self.instant) = (lines, rate, min_speed, instant);
    }

    pub fn active(&self) -> bool {
        self.target.is_some()
    }

    /// One wheel notch; `direction` is +1 (down) or -1 (up). Bursts accumulate.
    pub fn wheel(&mut self, direction: f64, max: f64) {
        if self.instant {
            self.target = None;
            self.pos = (self.pos.round() + direction * self.lines.round()).clamp(0.0, max);
            return;
        }
        let base = self.target.unwrap_or(self.pos);
        self.target = Some((base + direction * self.lines).clamp(0.0, max));
    }

    pub fn jump(&mut self, to: f64, max: f64) {
        self.target = None;
        self.pos = to.clamp(0.0, max);
    }

    /// Advance by `dt` seconds. Returns true if the position changed.
    pub fn tick(&mut self, dt: f64, max: f64) -> bool {
        let Some(t) = self.target else { return false };
        let target = t.min(max); // content may have shrunk
        let gap = target - self.pos;
        if gap.abs() < LAND {
            self.pos = target;
            self.target = None;
            return true;
        }
        let step = (gap.abs() * (1.0 - (-dt * self.rate).exp())).max(self.min_speed * dt);
        self.pos += step.min(gap.abs()).copysign(gap);
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn burst_accumulates_into_one_target() {
        let mut g = Glide::default();
        for _ in 0..5 {
            g.wheel(1.0, 1000.0);
        }
        assert!(g.active());
        let dt = 1.0 / GLIDE_FPS as f64;
        for _ in 0..(GLIDE_FPS * 2) {
            g.tick(dt, 1000.0);
        }
        assert!(!g.active());
        assert_eq!(g.pos, 15.0);
    }

    #[test]
    fn never_moves_less_than_min_speed_until_landing() {
        let mut g = Glide::default();
        g.wheel(1.0, 1000.0);
        let dt = 1.0 / GLIDE_FPS as f64;
        let mut prev = g.pos;
        while g.active() {
            g.tick(dt, 1000.0);
            let d = g.pos - prev;
            assert!(d >= GLIDE_MIN_SPEED * dt - 1e-9 || !g.active(), "crawl {d}");
            prev = g.pos;
        }
    }

    #[test]
    fn instant_mode_lands_in_one_step_on_whole_lines() {
        let mut g = Glide::default();
        g.preset(1);
        g.wheel(1.0, 1000.0);
        assert!(!g.active());
        assert_eq!(g.pos, 3.0);
        g.wheel(1.0, 1000.0);
        assert_eq!(g.pos, 6.0);
        g.wheel(-1.0, 1000.0);
        assert_eq!(g.pos, 3.0);
    }

    #[test]
    fn clamps_at_edges() {
        let mut g = Glide::default();
        g.wheel(-1.0, 100.0);
        assert!(g.active());
        g.tick(0.01, 100.0);
        assert_eq!(g.pos, 0.0);
    }
}
