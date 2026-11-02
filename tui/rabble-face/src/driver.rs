//! Behaviour over time (ui/face.py `Life`, `MOODS`, and the mood/pose half of `FaceWidget`), with no
//! clock and no terminal: the caller passes `now` and `dt`.

use crate::degrade::{break_level, eye_block, noise_level, DegradeState};
use crate::face::{blend_pose, Expr, Pose};
use crate::prng::SplitMix64;

pub const FPS: f64 = 20.0; // mini mark
pub const FULL_FPS: f64 = 30.0; // eyes, blink, gaze
pub const BG_FPS: f64 = 8.0;
pub const SPRING: f64 = 8.0; // pose ease rate per second
pub const DENIED_SECONDS: f64 = 2.0;

/// Drives blink and gaze over time so the face never sits still.
#[derive(Debug, Clone)]
pub struct Life {
    rng: SplitMix64,
    next_blink: f64,
    blink_t: f64,
    double: bool,
    pub gaze: [f64; 2],
    target: [f64; 2],
    next_sacc: f64,
    /// >1 = quicker saccades (the "working" mood).
    pub speed: f64,
    /// An outside gaze target and the time it lapses; while set it replaces the random saccades.
    external: Option<([f64; 2], f64)>,
}

impl Life {
    pub fn new(seed: i64) -> Life {
        Life { rng: SplitMix64::new(seed), next_blink: 2.5, blink_t: -1.0, double: false, gaze: [0.0; 2], target: [0.0; 2], next_sacc: 1.0, speed: 1.0, external: None }
    }

    /// Look at `target` (normalized, x and y in -1..1; clamped) until `until` (seconds on the `step` clock;
    /// `f64::INFINITY` = until cleared). `None` hands the gaze back to the seeded saccades.
    pub fn set_gaze(&mut self, target: Option<(f64, f64)>, until: f64) {
        self.external = target.map(|(x, y)| ([x.clamp(-1.0, 1.0), y.clamp(-1.0, 1.0)], until));
    }

    pub fn external_gaze(&self) -> Option<(f64, f64)> {
        self.external.map(|(g, _)| (g[0], g[1]))
    }

    /// -> (blink 0..1, gaze).
    pub fn step(&mut self, t: f64, dt: f64) -> (f64, (f64, f64)) {
        if self.blink_t < 0.0 && t >= self.next_blink {
            self.blink_t = 0.0;
            self.double = self.rng.random() < 0.25;
            self.next_blink = t + self.rng.uniform(2.5, 6.0);
        }
        let mut blink = 0.0;
        if self.blink_t >= 0.0 {
            self.blink_t += dt;
            let x = self.blink_t;
            if x < 0.16 {
                blink = (std::f64::consts::PI * x / 0.16).sin();
            } else if self.double && (0.24..0.40).contains(&x) {
                blink = (std::f64::consts::PI * (x - 0.24) / 0.16).sin();
            } else if x > 0.45 {
                self.blink_t = -1.0;
            }
        }
        if self.external.is_some_and(|(_, until)| t >= until) {
            self.external = None;
        }
        if let Some((g, _)) = self.external {
            self.target = g;
        } else if t >= self.next_sacc {
            self.target = [self.rng.uniform(-1.0, 1.0), self.rng.uniform(-0.8, 0.8)];
            self.next_sacc = t + self.rng.uniform(0.6, 2.2) / self.speed;
        }
        for i in 0..2 {
            self.gaze[i] += (self.target[i] - self.gaze[i]) * (dt * 9.0).min(1.0);
        }
        (blink, (self.gaze[0], self.gaze[1]))
    }
}

/// Engine state -> (expression, saccade speed).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Mood {
    Idle,
    Typing,
    Streaming,
    Working,
    Error,
    Denied,
}

impl Mood {
    pub const ALL: [Mood; 6] = [Mood::Idle, Mood::Typing, Mood::Streaming, Mood::Working, Mood::Error, Mood::Denied];

    pub fn name(self) -> &'static str {
        match self {
            Mood::Idle => "idle",
            Mood::Typing => "typing",
            Mood::Streaming => "streaming",
            Mood::Working => "working",
            Mood::Error => "error",
            Mood::Denied => "denied",
        }
    }

    pub fn from_name(n: &str) -> Mood {
        Mood::ALL.into_iter().find(|m| m.name() == n).unwrap_or(Mood::Idle)
    }

    pub fn info(self) -> (Expr, f64) {
        match self {
            Mood::Idle => (Expr::Calm, 1.0),
            Mood::Typing => (Expr::Listening, 1.0),
            Mood::Streaming => (Expr::Speaking, 1.0),
            Mood::Working => (Expr::Calm, 2.5),
            Mood::Error => (Expr::Sad, 1.0),
            Mood::Denied => (Expr::Angry, 1.0),
        }
    }
}

/// What the widget keeps between ticks: life, the eased pose, the signal (noise/breaking) and the glitch state.
pub struct Driver {
    pub life: Life,
    from_pose: Pose,
    from_expr: Expr,
    to_expr: Expr,
    blend: f64,
    revert_at: Option<f64>,
    pub noise: f64,
    pub breaking: f64,
    pub rng: SplitMix64,
    pub glitch: DegradeState,
    last: Option<f64>,
}

impl Default for Driver {
    fn default() -> Self {
        Driver::new()
    }
}

impl Driver {
    pub fn new() -> Driver {
        Driver {
            life: Life::new(3),
            from_pose: Expr::Calm.pose(),
            from_expr: Expr::Calm,
            to_expr: Expr::Calm,
            blend: 1.0,
            revert_at: None,
            noise: 0.0,
            breaking: 0.0,
            rng: SplitMix64::new(11),
            glitch: DegradeState::default(),
            last: None,
        }
    }

    /// The two inputs: the model's general capability and the session's stability, both 0-100.
    pub fn set_signal(&mut self, capability: f64, stability: f64) {
        self.noise = noise_level(capability);
        self.breaking = break_level(stability);
        if self.breaking <= 0.0 {
            self.glitch = DegradeState::default();
        }
    }

    pub fn block(&self) -> u32 {
        eye_block(self.noise)
    }

    /// Steer the gaze (see `Life::set_gaze`); `ttl` seconds from `t`, None = until cleared.
    pub fn set_gaze(&mut self, target: Option<(f64, f64)>, t: f64, ttl: Option<f64>) {
        self.life.set_gaze(target, ttl.map_or(f64::INFINITY, |s| t + s));
    }

    pub fn set_mood(&mut self, mood: Mood, now: f64) {
        let (expr, speed) = mood.info();
        self.life.speed = speed;
        self.revert_at = None;
        if mood == Mood::Denied {
            self.revert_at = Some(now + DENIED_SECONDS);
        }
        if expr != self.to_expr {
            self.from_pose = self.pose();
            self.from_expr = self.expr();
            self.to_expr = expr;
            self.blend = 0.0;
        }
    }

    pub fn target_expr(&self) -> Expr {
        self.to_expr
    }

    pub fn blend(&self) -> f64 {
        self.blend
    }

    pub fn pose(&self) -> Pose {
        blend_pose(&self.from_pose, &self.to_expr.pose(), self.blend)
    }

    /// The expression whose lid the frame uses: the target once the blend passes half way.
    pub fn expr(&self) -> Expr {
        if self.blend > 0.5 { self.to_expr } else { self.from_expr }
    }

    /// Advance to `now` (seconds on the caller's clock; `t` is seconds since the face started).
    /// -> (t, blink, gaze).
    pub fn tick(&mut self, now: f64, t: f64) -> (f64, (f64, f64)) {
        if let Some(at) = self.revert_at {
            if now >= at {
                self.set_mood(Mood::Idle, now);
            }
        }
        let dt = now - self.last.unwrap_or(now);
        self.last = Some(now);
        self.blend += (1.0 - self.blend) * (dt * SPRING).min(1.0);
        if self.blend > 0.995 {
            self.blend = 1.0;
        }
        let (blink, gaze) = self.life.step(t, dt);
        (blink, gaze)
    }
}
