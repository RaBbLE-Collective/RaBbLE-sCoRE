//! The entity (ui/face.py `Face`): rig, aura, envelope, mesh, orb, layered compose.

use crate::canvas::{Canvas, Cell, Grid};
use crate::eyes::{self, EyePalette, PaintOpts, Rig5, RigSet};
use crate::prng::SplitMix64;
use rabble_theme::{mix, Palette, Rgb};
use std::f64::consts::{PI, TAU};
use std::sync::Arc;

// face rig (alive-backend.js, face units)
pub const EYE_RX: f64 = 43.5;
pub const EYE_RY: f64 = 136.0;
pub const POR_RX: f64 = 117.0;
pub const POR_RY: f64 = 35.0;
pub const EYE_X: f64 = 92.0;
pub const POR_OFF: f64 = 126.0;
pub const GAZE_X: f64 = 26.0;
pub const GAZE_Y: f64 = 12.0;
pub const ROUGH_PX: f64 = 1.0;
pub const AURA_POLES: f64 = 0.08;
pub const AURA_RING: f64 = 0.16;
pub const ORB_R: f64 = 440.0;
pub const PROVIDER_MUTE: f64 = 0.35;

/// One part's transform in an expression.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Xf {
    pub dx: f64,
    pub dy: f64,
    pub rot: f64,
    pub sx: f64,
    pub sy: f64,
}

const ID: Xf = Xf { dx: 0.0, dy: 0.0, rot: 0.0, sx: 1.0, sy: 1.0 };

const fn x(dx: f64, dy: f64, rot: f64, sx: f64, sy: f64) -> Xf {
    Xf { dx, dy, rot, sx, sy }
}

/// Eyes L/R and portals L/R.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Pose {
    pub e_l: Xf,
    pub e_r: Xf,
    pub p_l: Xf,
    pub p_r: Xf,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Expr {
    Calm,
    Joy,
    Sad,
    Angry,
    Listening,
    Speaking,
}

impl Expr {
    pub const ALL: [Expr; 6] = [Expr::Calm, Expr::Joy, Expr::Sad, Expr::Angry, Expr::Listening, Expr::Speaking];

    pub fn name(self) -> &'static str {
        match self {
            Expr::Calm => "calm",
            Expr::Joy => "joy",
            Expr::Sad => "sad",
            Expr::Angry => "angry",
            Expr::Listening => "listening",
            Expr::Speaking => "speaking",
        }
    }

    pub fn from_name(n: &str) -> Option<Expr> {
        Expr::ALL.into_iter().find(|e| e.name() == n)
    }

    pub fn pose(self) -> Pose {
        match self {
            Expr::Calm => Pose { e_l: ID, e_r: ID, p_l: ID, p_r: ID },
            Expr::Joy => Pose {
                e_l: x(0.0, -6.0, -6.0, 1.05, 0.86), e_r: x(0.0, -6.0, 6.0, 1.05, 0.86),
                p_l: x(0.0, -20.0, -8.0, 1.06, 1.0), p_r: x(0.0, -56.0, 8.0, 1.08, 1.0),
            },
            Expr::Sad => Pose {
                e_l: x(0.0, 14.0, 7.0, 0.96, 0.9), e_r: x(0.0, 18.0, -7.0, 0.96, 0.9),
                p_l: x(0.0, 12.0, 10.0, 0.96, 1.0), p_r: x(0.0, -12.0, -6.0, 0.97, 1.0),
            },
            Expr::Angry => Pose {
                e_l: x(0.0, 4.0, -6.0, 1.03, 0.86), e_r: x(0.0, -4.0, 6.0, 1.03, 0.86),
                p_l: x(8.0, 40.0, -12.0, 1.08, 1.0), p_r: x(-8.0, -40.0, 12.0, 1.08, 1.0),
            },
            Expr::Listening => Pose {
                e_l: x(0.0, 0.0, 0.0, 1.0, 1.04), e_r: x(0.0, 0.0, 0.0, 1.0, 1.04),
                p_l: x(0.0, 252.0, 0.0, 1.0, 1.0), p_r: x(0.0, -252.0, 0.0, 1.0, 1.0),
            },
            Expr::Speaking => Pose {
                e_l: ID, e_r: ID,
                p_l: x(0.0, -44.0, 0.0, 1.05, 1.0), p_r: x(0.0, 44.0, 0.0, 1.05, 1.0),
            },
        }
    }

    /// Lids for the mini mark: (depth fraction of the eye's half-height, slope).
    pub fn lid(self) -> Option<(f64, f64)> {
        match self {
            Expr::Angry => Some((0.55, 0.9)),
            Expr::Sad => Some((0.45, -0.7)),
            _ => None,
        }
    }
}

fn blend_xf(a: Xf, b: Xf, p: f64) -> Xf {
    let l = |a: f64, b: f64| a + (b - a) * p;
    Xf { dx: l(a.dx, b.dx), dy: l(a.dy, b.dy), rot: l(a.rot, b.rot), sx: l(a.sx, b.sx), sy: l(a.sy, b.sy) }
}

/// Linear blend of two poses.
pub fn blend_pose(a: &Pose, b: &Pose, p: f64) -> Pose {
    Pose { e_l: blend_xf(a.e_l, b.e_l, p), e_r: blend_xf(a.e_r, b.e_r, p), p_l: blend_xf(a.p_l, b.p_l, p), p_r: blend_xf(a.p_r, b.p_r, p) }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Provider {
    Anthropic,
    Google,
    OpenAi,
}

impl Provider {
    pub fn name(self) -> &'static str {
        match self {
            Provider::Anthropic => "anthropic",
            Provider::Google => "google",
            Provider::OpenAi => "openai",
        }
    }
    pub fn from_name(n: &str) -> Option<Provider> {
        [Provider::Anthropic, Provider::Google, Provider::OpenAi].into_iter().find(|p| p.name() == n)
    }
    /// The providers' own brand colours, from the palette file's `brand_*` entries, read clockwise from the
    /// right. A palette without them leaves the aura in plain violet.
    fn brand(self, pal: &FacePalette) -> Vec<Rgb> {
        match self {
            Provider::Anthropic => vec![pal.brand_anthropic],
            Provider::Google => pal.brand_google.to_vec(),
            Provider::OpenAi => vec![pal.brand_openai],
        }
    }
}

/// Provider key for a model id, None for local models and anything unrecognised. No model named means
/// Claude Code's own default.
pub fn provider_of(model: Option<&str>, local: bool) -> Option<Provider> {
    if local {
        return None;
    }
    let m = model.unwrap_or("").to_lowercase();
    if m.is_empty() || ["claude", "opus", "sonnet", "haiku", "fable"].iter().any(|k| m.contains(k)) {
        return Some(Provider::Anthropic);
    }
    if ["gemini", "gemma"].iter().any(|k| m.contains(k)) {
        return Some(Provider::Google);
    }
    if ["gpt", "o1", "o3", "o4", "chatgpt", "codex"].iter().any(|k| m.starts_with(k)) || m.contains("gpt-") {
        return Some(Provider::OpenAi);
    }
    None
}

/// Colour at `angle` (radians, 0 = right, pi/2 = down) around a ring of stops.
fn ring_colour(ring: &[Rgb], angle: f64) -> Rgb {
    let n = ring.len();
    if n == 1 {
        return ring[0];
    }
    let pos = angle.rem_euclid(TAU) / TAU * n as f64;
    let k = pos as usize % n;
    mix(ring[k], ring[(k + 1) % n], pos - pos.trunc())
}

/// The colours the face reads from the palette.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct FacePalette {
    pub bg: Rgb,
    pub text: Rgb,
    pub cyan: Rgb,
    pub magenta: Rgb,
    pub violet: Rgb,
    pub pink: Rgb,
    pub muted: Rgb,
    pub brand_anthropic: Rgb,
    pub brand_google: [Rgb; 4],
    pub brand_openai: Rgb,
}

impl FacePalette {
    pub fn from_palette(p: &Palette) -> FacePalette {
        let violet = p.req("violet");
        let brand = |n: &str| p.get(n).unwrap_or(violet);
        FacePalette {
            bg: p.req("bg"), text: p.req("text"), cyan: p.req("cyan"), magenta: p.req("magenta"), violet, pink: p.req("pink"), muted: p.req("muted"),
            brand_anthropic: brand("brand_anthropic"),
            brand_google: [brand("brand_google_right"), brand("brand_google_bottom"), brand("brand_google_left"), brand("brand_google_top")],
            brand_openai: brand("brand_openai"),
        }
    }
    fn eyes(&self) -> EyePalette {
        EyePalette::new(self.bg, self.text, self.cyan, self.magenta)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Detail {
    Full,
    Mini,
}

#[derive(Debug, Clone, Copy, PartialEq)]
enum EdgeEnd {
    Node(usize),
    Tip,
}

fn lattice(seed: i64, i: i64) -> f64 {
    SplitMix64::new(seed * 7919 + i).random()
}

/// Cheap 1D value noise for the lightning strands.
fn noise(seed: i64, x: f64) -> f64 {
    let i = x.floor();
    let f = x - i;
    let (a, b) = (lattice(seed, i as i64), lattice(seed, i as i64 + 1));
    let f = f * f * (3.0 - 2.0 * f);
    a + (b - a) * f - 0.5
}

fn ellipse_pts(cx: f64, cy: f64, rx: f64, ry: f64, rot: f64, a0: f64, a1: f64, n: Option<usize>) -> Vec<(f64, f64)> {
    let n = n.unwrap_or_else(|| 24usize.max(((rx + ry) * 4.0) as usize));
    let (c, s) = (rot.cos(), rot.sin());
    (0..=n)
        .map(|i| {
            let a = a0 + (a1 - a0) * i as f64 / n as f64;
            let (u, v) = (rx * a.cos(), ry * a.sin());
            (cx + u * c - v * s, cy + u * s + v * c)
        })
        .collect()
}

/// The fast layer of the full face: each eye's halo tint and the eye/portal cells for one rig.
#[derive(Debug)]
pub struct Layer {
    key: Vec<i64>,
    rig: RigSet,
    pub rough: bool,
    pub tints: Vec<(usize, usize, Rgb)>,
    pub eyes: Vec<(usize, usize, Cell)>,
}

fn rig_arr(r: &RigSet) -> [Rig5; 4] {
    [r.e_l, r.e_r, r.p_l, r.p_r]
}

pub struct Face {
    pub cols: usize,
    pub rows: usize,
    pub pal: FacePalette,
    pub detail: Detail,
    pub expr: Expr,
    /// An explicit pose (a blend between expressions); overrides `expr`'s.
    pub pose: Option<Pose>,
    pub seed: i64,
    pub safe: bool,
    pub octant: bool,
    cx: f64,
    cy: f64,
    r: f64,
    hw: f64,
    hh: f64,
    k: f64,
    /// Physical height over width of one grid pixel (cell aspect / 2; 1.0 on the usual 2:1 cell).
    ay: f64,
    mins_erx: f64,
    mins_prx: f64,
    mins_pry: f64,
    spread: f64,
    aura_k: f64,
    lids: bool,
    pub rim_w: f64,
    halo: f64,
    edge: f64,
    /// Eye resolution step: 1 fine, 2 coarse, 3 chunky.
    pub block: u32,
    provider: Option<Provider>,
    nodes: Vec<(f64, f64, i32)>,
    edges: Vec<(usize, EdgeEnd)>,
}

impl Face {
    pub fn new(cols: usize, rows: usize, pal: FacePalette, detail: Detail, safe: bool) -> Face {
        Face::with_aspect(cols, rows, pal, detail, safe, 2.0)
    }

    /// `cell_aspect` is a terminal cell's height over its width in pixels (about 2; non-finite or non-positive
    /// falls back to 2). The full face lays its sphere, eyes and portals out in physical units, so the sphere
    /// is a true circle and the eyes keep their aspect on any cell shape. The mini mark stays at 2:1.
    pub fn with_aspect(cols: usize, rows: usize, pal: FacePalette, detail: Detail, safe: bool, cell_aspect: f64) -> Face {
        let full = detail == Detail::Full;
        let octant = !full;
        let (w, h) = if octant { (cols as f64 * 2.0, rows as f64 * 4.0) } else { (cols as f64, rows as f64 * 2.0) };
        let ay = if full && cell_aspect.is_finite() && cell_aspect > 0.0 { (cell_aspect / 2.0).clamp(0.25, 4.0) } else { 1.0 };
        let (r, hw, hh, k);
        if full {
            // One uniform scale for the whole rig, set by the smaller dimension: the sphere is a true circle of
            // ORB_R face units, which envelops every pose. `r` is in isotropic units (x pixels); a pixel is
            // `ay` times taller than wide, so the vertical room is (h / 2 - 1) * ay.
            r = (w / 2.0 - 1.0).min((h / 2.0 - 1.0) * ay).max(1.0);
            hw = w / 2.0 - 0.5;
            hh = (h * 0.49).min(r * 1.38 / ay);
            k = r / ORB_R;
        } else {
            r = h * 0.48;
            hw = 0.0;
            hh = 0.0;
            k = (h * 0.33) / EYE_RY;
        }
        let mut f = Face {
            cols, rows, pal, detail, expr: Expr::Calm, pose: None, seed: 7, safe, octant,
            cx: w / 2.0, cy: h / 2.0, r, hw, hh, k, ay,
            // the full face has no minimum sizes: a floor on one axis would stretch the eyes at small scales
            mins_erx: if full { 0.0 } else { 4.0 },
            mins_prx: if full { 0.0 } else { 7.0 },
            mins_pry: if full { 0.0 } else { 2.4 },
            spread: 1.0,
            aura_k: if full { 1.0 } else { 0.0 },
            lids: !full,
            rim_w: 1.0,
            halo: if full { 0.6 } else { 0.45 },
            edge: if full { 0.8 } else { 0.55 },
            block: 1,
            provider: None,
            nodes: Vec::new(),
            edges: Vec::new(),
        };
        if full {
            f.build_mesh();
        }
        f
    }

    pub fn from_theme(cols: usize, rows: usize, p: &Palette, detail: Detail, safe: bool) -> Face {
        Face::new(cols, rows, FacePalette::from_palette(p), detail, safe)
    }

    /// The sphere as (cx, cy, rx, ry) in grid pixels (half-block pixels on the full face), for fit checks.
    pub fn orb_extent(&self) -> (f64, f64, f64, f64) {
        (self.cx, self.cy, self.r, self.r / self.ay)
    }

    /// Physical height over width of one grid pixel the face was laid out for (cell aspect / 2).
    pub fn pixel_aspect(&self) -> f64 {
        self.ay
    }

    pub fn set_provider(&mut self, p: Option<Provider>) {
        self.provider = p;
    }

    pub fn provider(&self) -> Option<Provider> {
        self.provider
    }

    fn aura_ring(&self) -> Vec<Rgb> {
        self.provider.map_or(Vec::new(), |p| p.brand(&self.pal).into_iter().map(|c| mix(c, self.pal.violet, PROVIDER_MUTE)).collect())
    }

    fn env_h(&self, x: f64) -> f64 {
        let u = (x.abs() / self.hw).min(1.0);
        self.hh * (1.0 - u.powf(2.2)).powf(0.85)
    }

    /// Mesh nodes are fixed per instance; they shimmer, not move.
    fn build_mesh(&mut self) {
        let mut rng = SplitMix64::new(self.seed);
        let (mut nodes, mut edges): (Vec<(f64, f64, i32)>, Vec<(usize, EdgeEnd)>) = (Vec::new(), Vec::new());
        for side in [-1i32, 1] {
            let start = nodes.len();
            let count = 8usize.max((self.hw / 1.9) as usize);
            for _ in 0..count {
                for _try in 0..30 {
                    let x = rng.uniform(self.r * 0.75, self.hw * 0.9);
                    let lim = self.env_h(x) * 0.78;
                    let y = rng.uniform(-lim, lim);
                    let s = side as f64;
                    if nodes[start..].iter().all(|&(nx, ny, _)| (x * s - nx).hypot(y - ny) > 2.0) {
                        nodes.push((x * s, y, side));
                        break;
                    }
                }
            }
            let mine: Vec<usize> = (start..nodes.len()).collect();
            for &i in &mine {
                let (xi, yi, _) = nodes[i];
                let mut sorted = mine.clone();
                sorted.sort_by(|&a, &b| {
                    let da = (nodes[a].0 - xi).hypot(nodes[a].1 - yi);
                    let db = (nodes[b].0 - xi).hypot(nodes[b].1 - yi);
                    da.partial_cmp(&db).unwrap()
                });
                let near: Vec<usize> = sorted.iter().skip(1).take(5).copied().collect();
                let take = *rng.choice(&[3usize, 3, 4]);
                let mut picks: Vec<usize> = near.iter().take(take).copied().collect();
                if rng.random() < 0.25 {
                    picks.push(*rng.choice(&mine));
                }
                for j in picks {
                    if !edges.iter().any(|&(a, b)| a == j && b == EdgeEnd::Node(i)) {
                        edges.push((i, EdgeEnd::Node(j)));
                    }
                }
            }
            // a few long strands out to the envelope tip
            for i in rng.sample(&mine, 4.min(mine.len())) {
                edges.push((i, EdgeEnd::Tip));
            }
        }
        self.nodes = nodes;
        self.edges = edges;
    }

    pub fn mesh_nodes(&self) -> &[(f64, f64, i32)] {
        &self.nodes
    }

    /// Colour across the body: cyan on the left, magenta on the right.
    fn side(&self, x: f64) -> Rgb {
        let span = if self.hw != 0.0 { self.hw } else { self.r };
        let u = (x - self.cx) / span.max(1.0);
        mix(self.pal.cyan, self.pal.magenta, 0.5 + u * 0.9)
    }

    /// The four ellipses in px. `snap` rounds to whole pixels (the old half-block drawing).
    pub fn rig(&self, t: f64, blink: f64, gaze: (f64, f64), snap: bool) -> RigSet {
        let pose = self.pose.unwrap_or_else(|| self.expr.pose());
        let k = self.k;
        let (gx, gy) = gaze;
        let w0 = (EYE_RX * k).max(self.mins_erx) / (EYE_RX * k);
        let full = self.detail == Detail::Full;
        let float_y = if full { 12.0 * (1.05 * t).sin() } else { 0.0 };
        let bob = if full { 9.0 * (1.57 * t).sin() } else { 0.0 };
        let mut out = [(0.0, 0.0, 0.0, 0.0, 0.0); 4]; // eL eR pL pR
        for (idx, (side, ep, pp)) in [(-1.0f64, pose.e_l, pose.p_l), (1.0, pose.e_r, pose.p_r)].into_iter().enumerate() {
            let mut ex = self.cx + side * EYE_X * self.spread * k * w0 + (ep.dx + gx * GAZE_X) * k;
            let ey = self.cy * self.ay + (ep.dy + gy * GAZE_Y + float_y) * k;
            let mut erx = EYE_RX * ep.sx * k;
            let ery = EYE_RY * ep.sy * k;
            let por_y = self.cy * self.ay + (side * POR_OFF + pp.dy + float_y + side * bob) * k;
            let prx = POR_RX * pp.sx * k * w0;
            let pry = (prx * (POR_RY / POR_RX) * pp.sy / pp.sx).max(if full { 0.0 } else { 1.2 });
            let mut px = self.cx + side * EYE_X * self.spread * k * w0 + pp.dx * k;
            let b = blink * 0.93;
            let ery_b = ery * (1.0 - b);
            let ey_b = ey + (por_y - ey) * b;
            if snap {
                erx = erx.round_ties_even().max(self.mins_erx);
                ex = ex.round_ties_even();
                px = px.round_ties_even();
            } else {
                erx = erx.max(self.mins_erx);
            }
            out[idx] = (ex, ey_b, erx, ery_b.max(0.6), ep.rot.to_radians());
            out[2 + idx] = (px, por_y, prx.max(self.mins_prx), pry.max(self.mins_pry), pp.rot.to_radians());
        }
        RigSet { e_l: out[0], e_r: out[1], p_l: out[2], p_r: out[3] }
    }

    /// aura(x, y, base): a halo around each eye, from its edge outward, tinted by the provider.
    fn aura<'a>(&'a self, rig: &'a RigSet) -> impl Fn(f64, f64, Rgb) -> Rgb + 'a {
        let ring = self.aura_ring();
        let strength = (if ring.is_empty() { AURA_POLES } else { AURA_RING }) * self.aura_k;
        let ay = self.ay;
        // x, y arrive in grid pixels; the rig is isotropic
        move |x, y, base| {
            let y = y * ay;
            let mut c = base;
            for (e, pole) in [(rig.e_l, self.pal.cyan), (rig.e_r, self.pal.magenta)] {
                let (ex, ey, rx, ry, _) = e;
                let (dx, dy) = (x - ex, y - ey);
                let u = (dx / rx).hypot(dy / ry);
                if u <= 1.0 {
                    continue;
                }
                let v = (dx / (rx * 2.6)).hypot(dy / (ry * 1.55));
                if v >= 1.0 {
                    continue;
                }
                let col = if ring.is_empty() { pole } else { mix(pole, ring_colour(&ring, dy.atan2(dx)), 0.75) };
                c = mix(c, col, strength * (1.0 - v).powf(1.6) * ((u - 1.0) * 3.0).min(1.0));
            }
            // the halo's far tail is the void: leave it to the terminal's own background
            if c.0.abs_diff(base.0).max(c.1.abs_diff(base.1)).max(c.2.abs_diff(base.2)) <= 3 { base } else { c }
        }
    }

    /// Cell rows/cols (r0, r1, c0, c1) the halos can reach, half-block grid.
    fn aura_box(&self, rig: &RigSet) -> (usize, usize, usize, usize) {
        let es = [rig.e_l, rig.e_r];
        let fold = |f: &dyn Fn(&Rig5) -> f64, lo: bool| es.iter().map(f).fold(if lo { f64::INFINITY } else { f64::NEG_INFINITY }, |a, b| if lo { a.min(b) } else { a.max(b) });
        let x0 = fold(&|e| e.0 - e.2 * 2.6, true);
        let x1 = fold(&|e| e.0 + e.2 * 2.6, false);
        let y0 = fold(&|e| e.1 - e.3 * 1.55, true) / self.ay;
        let y1 = fold(&|e| e.1 + e.3 * 1.55, false) / self.ay;
        let lo = |v: f64| if v < 0.0 { 0 } else { v as usize };
        (
            lo((y0 / 2.0).floor() - 1.0),
            (((y1 / 2.0).floor() + 1.0).max(0.0) as usize).min(self.rows - 1),
            lo(x0.floor() - 1.0),
            ((x1.floor() + 1.0).max(0.0) as usize).min(self.cols - 1),
        )
    }

    /// The slow layers (envelope, mesh, orb) as plain cells: no eyes, no aura.
    pub fn background(&self, t: f64) -> Grid {
        let mut cv = Canvas::new(self.cols, self.rows, self.pal.bg, self.octant, self.safe);
        if self.detail == Detail::Full {
            self.envelope(&mut cv, t);
            self.mesh_draw(&mut cv, t);
        }
        self.orb(&mut cv, t);
        cv.cells()
    }

    fn paint_opts(&self, supersample: bool, fine: f64, lid: Option<(f64, f64)>) -> PaintOpts {
        PaintOpts {
            fine, n: 2.0, lids: [lid, lid], rim_w: self.rim_w, block: self.block, supersample,
            safe: self.safe, halo: self.halo, edge: self.edge, ay: self.ay,
        }
    }

    /// The fast layer of the full face for the rig at `t`. While the rig holds still `last` is returned
    /// as is; while the eyes travel (more than ROUGH_PX since `last`) edge supersampling is skipped, and
    /// the first still call repaints them clean.
    pub fn layer(&self, t: f64, blink: f64, gaze: (f64, f64), last: Option<&Arc<Layer>>) -> Arc<Layer> {
        let rig = self.rig(t, blink, gaze, false);
        let arr = rig_arr(&rig);
        let mut key: Vec<i64> = Vec::with_capacity(22);
        for a in &arr {
            key.extend([a.0, a.1, a.2, a.3].map(|v| (v * 2.0).round_ties_even() as i64));
        }
        key.extend(arr.iter().map(|a| (a.4 * 200.0).round_ties_even() as i64));
        key.push((self.rim_w * 1000.0) as i64);
        key.push(self.block as i64);
        if let Some(l) = last {
            if l.key == key && !l.rough {
                return l.clone();
            }
        }
        let rough = last.is_some_and(|l| {
            l.key != key && arr.iter().zip(rig_arr(&l.rig)).any(|(a, b)| (a.0 - b.0).abs() > ROUGH_PX || (a.1 - b.1).abs() > ROUGH_PX || (a.2 - b.2).abs() > ROUGH_PX || (a.3 - b.3).abs() > ROUGH_PX)
        });
        let pal = &self.pal;
        let mut bg = vec![vec![pal.bg; self.cols]; self.rows];
        let aura = self.aura(&rig);
        let (r0, r1, c0, c1) = self.aura_box(&rig);
        let mut tints = Vec::new();
        for r in r0..=r1 {
            for c in c0..=c1 {
                let col = aura(c as f64 + 0.5, (r as f64 + 0.5) * 2.0, pal.bg);
                if col != pal.bg {
                    bg[r][c] = col;
                    tints.push((r, c, col));
                }
            }
        }
        let eyes = eyes::paint(&bg, &rig, &pal.eyes(), &self.paint_opts(!rough, 2.0, None));
        Arc::new(Layer { key, rig, rough, tints, eyes })
    }

    /// Cells of the full face: the background `base` with the fast `layer` on top.
    pub fn assemble(&self, base: &Grid, layer: Option<&Layer>) -> Grid {
        let mut grid = base.clone();
        let Some(layer) = layer else { return grid };
        let bg = self.pal.bg;
        for &(r, c, col) in &layer.tints {
            let cell = &mut grid[r][c];
            if cell.ch == ' ' {
                cell.fg = col;
            }
            if cell.bg == bg {
                cell.bg = col;
            }
        }
        for &(r, c, cell) in &layer.eyes {
            grid[r][c] = cell;
        }
        grid
    }

    /// layer + assemble in one call; `base` defaults to a fresh background.
    pub fn compose(&self, t: f64, blink: f64, gaze: (f64, f64), base: Option<&Grid>, last: Option<&Arc<Layer>>) -> (Grid, Arc<Layer>) {
        let layer = self.layer(t, blink, gaze, last);
        let grid = match base {
            Some(b) => self.assemble(b, Some(&layer)),
            None => self.assemble(&self.background(t), Some(&layer)),
        };
        (grid, layer)
    }

    /// The whole picture on one canvas (the mini mark, and the reference for the layered path). Stateless:
    /// the eyes are always painted clean.
    pub fn frame(&self, t: f64, blink: f64, gaze: (f64, f64)) -> Grid {
        let mut cv = Canvas::new(self.cols, self.rows, self.pal.bg, self.octant, self.safe);
        let rig = self.rig(t, blink, gaze, false);
        let lid = if self.lids { self.expr.lid() } else { None };
        let aura = self.aura(&rig);
        cv.glow(&aura);
        if self.detail == Detail::Full {
            self.envelope(&mut cv, t);
            self.mesh_draw(&mut cv, t);
        }
        self.orb(&mut cv, t);
        let cells = eyes::paint(&cv.bg, &rig, &self.pal.eyes(), &self.paint_opts(true, if self.octant { 1.0 } else { 2.0 }, lid));
        cv.over.extend(cells);
        cv.cells()
    }

    fn envelope(&self, cv: &mut Canvas, t: f64) {
        let p = &self.pal;
        for (strand, (scale, w, seed)) in [(1.0, 1.0, 1i64), (0.92, 0.8, 2), (0.84, 0.6, 3)].into_iter().enumerate() {
            let mut prev: [Option<(f64, f64)>; 2] = [None, None];
            let steps = (self.hw * 2.0 * 2.0) as usize;
            for i in 0..=steps {
                let xx = -self.hw + 2.0 * self.hw * i as f64 / steps as f64;
                let jag = noise(seed, xx * 0.45 + t * 1.3) * 1.6 + noise(seed + 9, xx * 2.6 - t * 3.1) * 0.9;
                for (si, sgn) in [-1.0f64, 1.0].into_iter().enumerate() {
                    let y = sgn * (self.env_h(xx) * scale + jag * (self.env_h(xx) / self.hh.max(1.0)));
                    let (px, py) = (self.cx + xx, self.cy + y);
                    let col = mix(mix(p.text, self.side(px), 0.2 + 0.3 * strand as f64), p.bg, 0.15 * strand as f64);
                    if let Some((qx, qy)) = prev[si] {
                        cv.line(qx, qy, px, py, col, w);
                    }
                    prev[si] = Some((px, py));
                }
            }
        }
    }

    fn mesh_draw(&self, cv: &mut Canvas, t: f64) {
        let p = &self.pal;
        let tip = |side: i32| if side < 0 { (self.cx - self.hw, self.cy) } else { (self.cx + self.hw, self.cy) };
        let pos: Vec<(f64, f64)> = self.nodes.iter().enumerate().map(|(i, &(x, y, _))| (self.cx + x, self.cy + y + (t * 0.9 + i as f64).sin() * 0.25)).collect();
        for &(a, b) in &self.edges {
            let (x0, y0) = pos[a];
            let (x1, y1) = match b {
                EdgeEnd::Tip => tip(self.nodes[a].2),
                EdgeEnd::Node(j) => pos[j],
            };
            let side_col = if self.nodes[a].2 < 0 { p.cyan } else { p.magenta };
            cv.line(x0, y0, x1, y1, mix(side_col, p.bg, 0.12), 0.5);
        }
        for (i, &(x, y)) in pos.iter().enumerate() {
            let side_col = if self.nodes[i].2 < 0 { p.cyan } else { p.magenta };
            let twinkle = 0.5 + 0.5 * (t * 2.3 + i as f64 * 1.7).sin();
            cv.px(x, y, mix(side_col, p.text, 0.25 + 0.5 * twinkle));
        }
    }

    fn orb(&self, cv: &mut Canvas, t: f64) {
        let p = &self.pal;
        let (cx, cy, r) = (self.cx, self.cy, self.r);
        let ry = r / self.ay; // vertical radius in pixels: the sphere is round in physical units
        let dim = if self.detail == Detail::Full { 0.55 } else { 0.75 };
        for (x, y) in ellipse_pts(cx, cy, r, ry, 0.0, 0.0, TAU, None) {
            cv.dot(x, y, mix(self.side(x), p.bg, dim * 0.6), 0.7);
        }
        if self.detail != Detail::Full {
            return;
        }
        for m in 0..5 {
            let a = t * 0.25 + m as f64 * PI / 5.0;
            let rx = a.cos().abs() * r;
            if rx < 0.6 {
                continue;
            }
            let pts = if a.cos() > 0.0 { ellipse_pts(cx, cy, rx, ry, 0.0, -PI / 2.0, PI / 2.0, None) } else { ellipse_pts(cx, cy, rx, ry, 0.0, PI / 2.0, 3.0 * PI / 2.0, None) };
            for (x, y) in pts {
                cv.dot(x, y, mix(self.side(x), p.bg, dim), 0.3);
            }
        }
        for lat in [-0.6f64, -0.3, 0.0, 0.3, 0.6] {
            let y0 = cy + ry * (lat * 1.2).sin();
            let rx = r * (lat * 1.2).cos();
            for (x, y) in ellipse_pts(cx, y0, rx, rx * 0.12 / self.ay, 0.0, 0.0, PI, None) {
                cv.dot(x, y, mix(self.side(x), p.bg, dim), 0.3);
            }
        }
        // motes inside
        let mut rng = SplitMix64::new(self.seed + 1);
        let cols = [p.violet, p.cyan, p.magenta, p.pink];
        for _ in 0..((r * 1.6) as usize) {
            let a = rng.uniform(0.0, TAU);
            let rr = r * rng.random().sqrt() * 0.95;
            let drift = t * 0.4 * (0.5 + rng.random());
            let (x, y) = (cx + rr * (a + drift).cos(), cy + rr * (a + drift).sin() * 0.9 / self.ay);
            let col = *rng.choice(&cols);
            cv.dot(x, y, mix(col, p.bg, 0.35 + 0.3 * rng.random()), 0.2);
        }
    }
}
