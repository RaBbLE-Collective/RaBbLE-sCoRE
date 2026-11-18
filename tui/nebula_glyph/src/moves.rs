//! The glyph's reduced moves: portal swap, turn and boot. Pure functions from progress to a pose delta.
//!
//! Seeds from the registry's `terminal` profile (rabble.entity.json 0.1.0): a move lasts 0.5 s and steps at
//! 8 Hz (`moves.portal_swap.params.reduced`: duration 0.5, glyph_step_hz 8). So a swap or turn is five frames:
//! progress 0, .25, .5, .75, 1. Boot takes the device's compressed timeline (scene.c 626-637 via the
//! registry's `timelines.boot.params.reconciliation`): cyan portal 0 to .35 s, magenta portal .12 s later,
//! eyes and rest .67 to .92 s, 1.0 s in all, same 8 Hz steps. Portals come first, then the eyes.
//!
//! Deviation, on purpose: the registry seeds the reduced swap with `arc_fu 0` (no arc). With no arc the two
//! portals (rx 117 fu, centres 184 fu apart) pass through each other at mid-swap, which Canon 2 forbids
//! (portals never touch). The glyph has no depth to put them at, so it spends the arc sideways: each portal
//! bows outward by `ARC_FU` at mid-swap. The eyes withdraw to nothing at mid-swap (`eye_squash_floor 0`).

use crate::face::{Pose, Xf, EYE_X, POR_OFF, POR_RX};
use std::f64::consts::PI;

pub const STEP_HZ: f64 = 8.0;
pub const SWAP_SECONDS: f64 = 0.5;
pub const TURN_SECONDS: f64 = 0.5;
pub const BOOT_SECONDS: f64 = 1.0;
/// Sideways bow of each portal at mid-swap, face units (the registry's web `arc_fu`, spent laterally).
pub const ARC_FU: f64 = 75.0;
/// A turning eye never narrows past this fraction of its width (a thin slit, never nothing).
pub const TURN_MIN_W: f64 = 0.2;
/// Boot windows in seconds: (cyan portal, magenta portal, eyes).
pub const BOOT_CYAN: (f64, f64) = (0.0, 0.35);
pub const BOOT_MAGENTA: (f64, f64) = (0.12, 0.47);
pub const BOOT_EYES: (f64, f64) = (0.67, 0.92);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Move {
    PortalSwap,
    Turn,
    Boot,
}

impl Move {
    pub fn name(self) -> &'static str {
        match self {
            Move::PortalSwap => "portal_swap",
            Move::Turn => "turn",
            Move::Boot => "boot",
        }
    }
    pub fn seconds(self) -> f64 {
        match self {
            Move::PortalSwap => SWAP_SECONDS,
            Move::Turn => TURN_SECONDS,
            Move::Boot => BOOT_SECONDS,
        }
    }
    /// Progress 0..1 at `elapsed` seconds, held to the glyph's 8 Hz steps.
    pub fn progress(self, elapsed: f64) -> f64 {
        let step = (elapsed.max(0.0) * STEP_HZ).floor() / STEP_HZ;
        (step / self.seconds()).min(1.0)
    }
}

pub fn smoothstep(x: f64) -> f64 {
    let x = x.clamp(0.0, 1.0);
    x * x * (3.0 - 2.0 * x)
}

fn window(t: f64, w: (f64, f64)) -> f64 {
    smoothstep((t - w.0) / (w.1 - w.0))
}

fn lerp(a: f64, b: f64, p: f64) -> f64 {
    a + (b - a) * p
}

/// The form with the portals' sides exchanged: left portal's offset becomes `+2*POR_OFF - dy`, the right's
/// `-2*POR_OFF - dy`. A swapped pose mirrors back to the rest one, so it is its own inverse.
pub fn mirror(p: &Pose) -> Pose {
    let m = |x: Xf, s: f64| Xf { dy: s * 2.0 * POR_OFF - x.dy, ..x };
    Pose { e_l: p.e_l, e_r: p.e_r, p_l: m(p.p_l, 1.0), p_r: m(p.p_r, -1.0) }
}

/// The pose `base` at swap progress `p` (0 = base, 1 = `mirror(base)`), with the lateral arc and the eyes
/// withdrawing at mid-swap.
pub fn swap_at(base: &Pose, p: f64) -> Pose {
    if p <= 0.0 {
        return *base;
    }
    if p >= 1.0 {
        return mirror(base);
    }
    let s = smoothstep(p);
    let end = mirror(base);
    let bow = ARC_FU * (PI * s).sin();
    let keep = 1.0 - (PI * s).sin(); // eye_squash_floor 0
    let mix = |a: Xf, b: Xf, dx: f64, sy: f64| Xf { dx: lerp(a.dx, b.dx, s) + dx, dy: lerp(a.dy, b.dy, s), rot: lerp(a.rot, b.rot, s), sx: lerp(a.sx, b.sx, s), sy: lerp(a.sy, b.sy, s) * sy };
    Pose { e_l: mix(base.e_l, end.e_l, 0.0, keep), e_r: mix(base.e_r, end.e_r, 0.0, keep), p_l: mix(base.p_l, end.p_l, -bow, 1.0), p_r: mix(base.p_r, end.p_r, bow, 1.0) }
}

/// `base` at turn progress `p`: the eyes trade places across the middle as thin slits. The eye on each side is
/// whichever is on that side, so colour (by side) never trades; the end frame equals the start.
pub fn turn_at(base: &Pose, p: f64) -> Pose {
    if p <= 0.0 || p >= 1.0 {
        return *base; // the end frame is the start
    }
    let c = (PI * p).cos();
    let w = c.abs().max(TURN_MIN_W);
    // eye A runs -EYE_X*c and eye B +EYE_X*c, so the left side always holds the one with x <= 0, at -EYE_X*|c|
    let slot = |side: f64, e: Xf| Xf { dx: e.dx + side * EYE_X * c.abs() - side * EYE_X, sx: e.sx * w, ..e };
    Pose { e_l: slot(-1.0, base.e_l), e_r: slot(1.0, base.e_r), p_l: base.p_l, p_r: base.p_r }
}

/// `base` at boot progress `p`: portals grow in (cyan, then magenta), then the eyes rise.
pub fn boot_at(base: &Pose, p: f64) -> Pose {
    if p >= 1.0 {
        return *base;
    }
    let t = p * BOOT_SECONDS;
    let (pc, pm, ey) = (window(t, BOOT_CYAN), window(t, BOOT_MAGENTA), window(t, BOOT_EYES));
    let por = |x: Xf, k: f64| {
        let k = k.max(0.02);
        Xf { sx: x.sx * k, sy: x.sy * k, ..x }
    };
    let eye = |x: Xf| Xf { sx: x.sx * lerp(0.85, 1.0, ey), sy: x.sy * lerp(0.02, 1.0, ey), ..x };
    Pose { e_l: eye(base.e_l), e_r: eye(base.e_r), p_l: por(base.p_l, pc), p_r: por(base.p_r, pm) }
}

/// The pose for `mv` at `progress`, from `base`.
pub fn pose_at(mv: Move, base: &Pose, progress: f64) -> Pose {
    match mv {
        Move::PortalSwap => swap_at(base, progress),
        Move::Turn => turn_at(base, progress),
        Move::Boot => boot_at(base, progress),
    }
}

/// Portal centres' x in face units for a pose (test and reading aid): (left, right).
pub fn portal_x(p: &Pose) -> (f64, f64) {
    (-EYE_X + p.p_l.dx, EYE_X + p.p_r.dx)
}

/// Half the portals' width at this pose, face units.
pub fn portal_rx(p: &Pose) -> (f64, f64) {
    (POR_RX * p.p_l.sx, POR_RX * p.p_r.sx)
}
