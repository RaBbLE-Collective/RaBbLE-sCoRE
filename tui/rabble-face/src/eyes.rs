//! Fine-resolution eyes and portals (ui/eyes.py): analytic shapes at octant resolution, anti-aliased by
//! supersampling, each cell fitted with its best two colours and an octant glyph. Pure functions:
//! rig tuples in, per-cell overrides out.

use crate::canvas::{octant_table, Cell};
use rabble_theme::Rgb;

type V3 = [f64; 3];
const FLAT: f64 = 5.0;

fn v3(c: Rgb) -> V3 {
    [c.0 as f64, c.1 as f64, c.2 as f64]
}

fn over(base: V3, top: V3, a: f64) -> V3 {
    if a <= 0.0 {
        return base;
    }
    if a >= 1.0 {
        return top;
    }
    [base[0] + (top[0] - base[0]) * a, base[1] + (top[1] - base[1]) * a, base[2] + (top[2] - base[2]) * a]
}

fn smooth(x: f64) -> f64 {
    let x = x.clamp(0.0, 1.0);
    x * x * (3.0 - 2.0 * x)
}

fn rnd(v: f64) -> u8 {
    v.round_ties_even().clamp(0.0, 255.0) as u8
}

fn rgb(v: V3) -> Rgb {
    Rgb(rnd(v[0]), rnd(v[1]), rnd(v[2]))
}

/// A rotated (super)ellipse with an approximate signed distance (negative inside, sub-pixels).
struct Shape {
    cx: f64,
    cy: f64,
    rx: f64,
    ry: f64,
    n: f64,
    c: f64,
    s: f64,
    reach_x: f64,
    reach_y: f64,
}

impl Shape {
    fn new(cx: f64, cy: f64, rx: f64, ry: f64, rot: f64, n: f64) -> Shape {
        let (rx, ry) = (rx.max(0.3), ry.max(0.3));
        Shape { cx, cy, rx, ry, n, c: rot.cos(), s: rot.sin(), reach_x: rx + 7.0, reach_y: ry + 7.0 }
    }

    fn sd(&self, x: f64, y: f64) -> f64 {
        let (dx, dy) = (x - self.cx, y - self.cy);
        let u = (dx * self.c + dy * self.s).abs() + 1e-9;
        let v = (-dx * self.s + dy * self.c).abs() + 1e-9;
        let n = self.n;
        let (a, b) = (u / self.rx, v / self.ry);
        let (f, g);
        if n == 2.0 {
            f = (a * a + b * b).sqrt();
            let (p, q) = (a / self.rx, b / self.ry);
            g = (p * p + q * q).sqrt() / f;
        } else {
            f = (a.powf(n) + b.powf(n)).powf(1.0 / n);
            let k = f.powf(1.0 - n);
            let (p, q) = (a.powf(n - 1.0) / self.rx * k, b.powf(n - 1.0) / self.ry * k);
            g = (p * p + q * q).sqrt();
        }
        (f - 1.0) / g.max(1e-6)
    }

    /// y of the upper/lower edge at column x (pulled in by `inset`), None outside.
    fn edge_y(&self, x: f64, lower: bool, inset: f64) -> Option<f64> {
        let rx = (self.rx - inset).max(0.3);
        let ry = (self.ry - inset).max(0.3);
        let u = (x - self.cx) / rx;
        if u.abs() >= 1.0 {
            return None;
        }
        let off = ry * (1.0 - u * u).sqrt();
        let t = if self.c != 0.0 { self.s / self.c } else { 0.0 };
        Some(self.cy + t * (x - self.cx) + if lower { off } else { -off })
    }
}

struct Pal {
    text: V3,
    void: V3,
}

struct Part {
    eye: Shape,
    portal: Shape,
    below: bool,
    col: V3,
    lid: Option<(f64, f64)>,
    inner: f64,
    halo: f64,
    edge: f64,
    eye_rim: f64,
    por_rim: f64,
    bbox: [f64; 4],
}

impl Part {
    #[allow(clippy::too_many_arguments)]
    fn new(eye: &Rig5, portal: &Rig5, col: V3, fine: f64, n: f64, lid: Option<(f64, f64)>, inner: f64, rim_w: f64, halo: f64, edge: f64) -> Part {
        let (ex, ey, erx, ery) = (eye.0 * fine, eye.1 * fine, eye.2 * fine, eye.3 * fine);
        let (px, py, prx, pry) = (portal.0 * fine, portal.1 * fine, portal.2 * fine, portal.3 * fine);
        let e = Shape::new(ex, ey, erx, ery, eye.4, n);
        let p = Shape::new(px, py, prx, pry, portal.4, 2.0);
        Part {
            eye: e,
            portal: p,
            below: py > ey,
            col,
            lid,
            inner,
            halo,
            edge,
            eye_rim: (0.7f64).max(erx * 2.0 * 0.069) * rim_w,
            por_rim: (0.9f64).max(pry * 2.0 * 0.085),
            bbox: [(ex - erx).min(px - prx), (ey - ery).min(py - pry), (ex + erx).max(px + prx), (ey + ery).max(py + pry)],
        }
    }

    fn sample(&self, pal: &Pal, x: f64, y: f64, base: V3) -> V3 {
        let col = self.col;
        let (eye, por) = (&self.eye, &self.portal);
        let mut c = base;
        let psd = if (x - por.cx).abs() < por.reach_x && (y - por.cy).abs() < por.reach_y { por.sd(x, y) } else { 99.0 };
        let mut front = 0.0;
        if psd < 6.0 {
            if psd > 0.0 {
                c = over(c, col, 0.12 * self.halo * (-psd / (1.2 * self.halo.max(0.05))).exp());
            }
            if psd <= 0.0 {
                c = over(c, pal.void, smooth(0.5 - psd));
            }
            let core = if psd.abs() < self.por_rim / 2.0 + self.edge { smooth(1.0 - (psd.abs() - self.por_rim / 2.0) / self.edge) } else { 0.0 };
            if core > 0.0 {
                let pc = if por.c != 0.0 { por.c } else { 1.0 };
                if self.below && y > por.cy + por.s / pc * (x - por.cx) {
                    front = core;
                } else {
                    c = over(c, col, core);
                }
            }
        }
        let esd = if (x - eye.cx).abs() < eye.reach_x && (y - eye.cy).abs() < eye.reach_y { eye.sd(x, y) } else { 99.0 };
        if esd < 6.0 {
            let mut visible;
            if self.below {
                visible = match por.edge_y(x, true, 0.0) {
                    Some(e) => y <= e,
                    None => y <= por.cy,
                };
            } else {
                visible = match por.edge_y(x, false, self.por_rim / 2.0) {
                    Some(e) => y >= e,
                    None => y >= por.cy,
                };
            }
            if visible {
                if let Some(lid) = self.lid {
                    let lid_y = eye.cy - eye.ry + lid.0 * eye.ry + lid.1 * (x - eye.cx) * self.inner;
                    if y < lid_y {
                        visible = false;
                    }
                }
                if visible {
                    if esd > 0.0 {
                        c = over(c, col, 0.22 * self.halo * (-esd / self.halo.max(0.05)).exp());
                    }
                    if esd <= 0.0 {
                        let depth = (-esd / eye.rx.max(1.0)).min(1.0);
                        let sclera = over(pal.text, col, 0.10 * (1.0 - depth) * (1.0 - depth));
                        c = over(c, sclera, smooth(0.5 - esd));
                    }
                    let half = self.eye_rim / 2.0;
                    if esd.abs() < half + self.edge {
                        c = over(c, col, smooth(1.0 - (esd.abs() - half) / self.edge));
                    }
                    if let Some(lid) = self.lid {
                        let d = y - (eye.cy - eye.ry + lid.0 * eye.ry + lid.1 * (x - eye.cx) * self.inner);
                        if (0.0..1.5).contains(&d) {
                            c = over(c, col, 1.0 - d / 1.5);
                        }
                    }
                }
            }
        }
        if front > 0.0 {
            c = over(c, col, front);
        }
        c
    }
}

/// One ellipse of the rig: (cx, cy, rx, ry, rot) in the face's pixel units.
pub type Rig5 = (f64, f64, f64, f64, f64);

/// The four ellipses.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RigSet {
    pub e_l: Rig5,
    pub e_r: Rig5,
    pub p_l: Rig5,
    pub p_r: Rig5,
}

/// The colours the eye renderer reads from the palette.
#[derive(Debug, Clone, Copy)]
pub struct EyePalette {
    pub bg: Rgb,
    pub text: Rgb,
    pub cyan: Rgb,
    pub magenta: Rgb,
    /// `P.setdefault("void", bg * 0.5)`.
    pub void: Rgb,
}

impl EyePalette {
    pub fn new(bg: Rgb, text: Rgb, cyan: Rgb, magenta: Rgb) -> EyePalette {
        let h = |v: u8| (v as f64 * 0.5).round_ties_even() as u8;
        EyePalette { bg, text, cyan, magenta, void: Rgb(h(bg.0), h(bg.1), h(bg.2)) }
    }
}

pub struct PaintOpts {
    pub fine: f64,
    pub n: f64,
    pub lids: [Option<(f64, f64)>; 2],
    pub rim_w: f64,
    pub block: u32,
    pub supersample: bool,
    pub safe: bool,
    pub halo: f64,
    pub edge: f64,
}

impl Default for PaintOpts {
    fn default() -> Self {
        PaintOpts { fine: 2.0, n: 2.0, lids: [None, None], rim_w: 1.0, block: 1, supersample: true, safe: false, halo: 1.0, edge: 0.8 }
    }
}

fn quads(subs: &[V3; 8]) -> [V3; 8] {
    let mut out = *subs;
    for a in [0usize, 1, 4, 5] {
        let m = [0, 1, 2].map(|k| (subs[a][k] + subs[a + 2][k]) / 2.0);
        out[a] = m;
        out[a + 2] = m;
    }
    out
}

fn d2(a: &V3, b: &V3) -> f64 {
    (a[0] - b[0]).powi(2) + (a[1] - b[1]).powi(2) + (a[2] - b[2]).powi(2)
}

fn mean(g: &[V3]) -> Rgb {
    let m = |k: usize| g.iter().map(|s| s[k]).sum::<f64>() / g.len() as f64;
    Rgb(rnd(m(0)), rnd(m(1)), rnd(m(2)))
}

/// 8 sub-pixel colours (row-major 2x4) -> (glyph, fg, bg): two clusters around the most distant pair.
fn fit(subs_in: &[V3; 8], safe: bool) -> (char, Rgb, Rgb) {
    let subs = if safe { quads(subs_in) } else { *subs_in };
    let (mut best, mut a, mut b) = (-1.0f64, subs[0], subs[0]);
    for i in 0..8 {
        for j in i + 1..8 {
            let d = d2(&subs[i], &subs[j]);
            if d > best {
                best = d;
                a = subs[i];
                b = subs[j];
            }
        }
    }
    if best < 90.0 {
        let m = mean(&subs);
        return (' ', m, m);
    }
    let (mut ins, mut fa, mut fb) = (0usize, Vec::new(), Vec::new());
    for (i, s) in subs.iter().enumerate() {
        if d2(s, &a) <= d2(s, &b) {
            ins |= 1 << i;
            fa.push(*s);
        } else {
            fb.push(*s);
        }
    }
    if best < 9000.0 && fa.len().min(fb.len()) <= 1 {
        let m = mean(&subs);
        return (' ', m, m);
    }
    let (mut ma, mut mb) = (mean(&fa), mean(&fb));
    let sum = |c: Rgb| c.0 as u32 + c.1 as u32 + c.2 as u32;
    if sum(ma) < sum(mb) {
        ins = !ins & 0xFF;
        std::mem::swap(&mut ma, &mut mb);
    }
    (octant_table()[ins], ma, mb)
}

/// Both eye/portal pairs as per-cell overrides. `bg` is the canvas's cell background (the aura glow), read only.
pub fn paint(bg_grid: &[Vec<Rgb>], rig: &RigSet, pal: &EyePalette, o: &PaintOpts) -> Vec<(usize, usize, Cell)> {
    let (ncols, nrows) = (bg_grid.first().map_or(0, |r| r.len()), bg_grid.len());
    let p = Pal { text: v3(pal.text), void: v3(pal.void) };
    let inner = [1.0, -1.0];
    let parts = [
        Part::new(&rig.e_l, &rig.p_l, v3(pal.cyan), o.fine, o.n, o.lids[0], inner[0], o.rim_w, o.halo, o.edge),
        Part::new(&rig.e_r, &rig.p_r, v3(pal.magenta), o.fine, o.n, o.lids[1], inner[1], o.rim_w, o.halo, o.edge),
    ];
    let x0 = parts.iter().map(|p| p.bbox[0]).fold(f64::INFINITY, f64::min) - 5.0;
    let y0 = parts.iter().map(|p| p.bbox[1]).fold(f64::INFINITY, f64::min) - 5.0;
    let x1 = parts.iter().map(|p| p.bbox[2]).fold(f64::NEG_INFINITY, f64::max) + 5.0;
    let y1 = parts.iter().map(|p| p.bbox[3]).fold(f64::NEG_INFINITY, f64::max) + 5.0;
    let c0 = ((x0 / 2.0).floor().max(0.0)) as usize;
    let c1 = ((x1 / 2.0).floor().min(ncols as f64 - 1.0)).max(-1.0);
    let r0 = ((y0 / 4.0).floor().max(0.0)) as usize;
    let r1 = ((y1 / 4.0).floor().min(nrows as f64 - 1.0)).max(-1.0);
    let has_lids = o.lids.iter().any(|l| l.is_some());
    let big = parts.iter().map(|p| p.eye.rx).fold(f64::INFINITY, f64::min) > 8.0;
    const SS_SMALL: [(f64, f64); 4] = [(0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75)];
    const SS_BIG: [(f64, f64); 2] = [(0.25, 0.5), (0.75, 0.5)];
    let taps: &[(f64, f64)] = if big { &SS_BIG } else { &SS_SMALL };
    let block = o.block as f64;
    let mut out = Vec::new();
    if c1 < 0.0 || r1 < 0.0 {
        return out;
    }
    for r in r0..=(r1 as usize) {
        for c in c0..=(c1 as usize) {
            let (cf, rf) = (c as f64, r as f64);
            let near: Vec<&Part> = parts
                .iter()
                .filter(|p| p.bbox[0] - 5.0 <= cf * 2.0 + 2.0 && cf * 2.0 <= p.bbox[2] + 5.0 && p.bbox[1] - 5.0 <= rf * 4.0 + 4.0 && rf * 4.0 <= p.bbox[3] + 5.0)
                .collect();
            if near.is_empty() {
                continue;
            }
            let base_rgb = bg_grid[r][c];
            let base = v3(base_rgb);
            let shade = |x: f64, y: f64| -> V3 {
                let mut col = base;
                for pt in &near {
                    col = pt.sample(&p, x, y, col);
                }
                col
            };
            if !has_lids && o.block == 1 {
                let (mx, my) = (cf * 2.0 + 1.0, rf * 4.0 + 2.0);
                if near.iter().all(|pt| pt.eye.sd(mx, my).abs() > FLAT && pt.portal.sd(mx, my).abs() > FLAT) {
                    let col = rgb(shade(mx, my));
                    if col != base_rgb {
                        out.push((r, c, Cell { ch: ' ', fg: col, bg: col }));
                    }
                    continue;
                }
            }
            let sub_at = |i: usize| (cf * 2.0 + (i % 2) as f64, rf * 4.0 + (i / 2) as f64);
            let mut subs = [[0.0; 3]; 8];
            let mut spread = 0.0;
            if o.block > 1 {
                let snap = |v: f64| (v / block).floor() * block + block / 2.0;
                for (i, s) in subs.iter_mut().enumerate() {
                    let (sx, sy) = sub_at(i);
                    *s = shade(snap(sx), snap(sy));
                }
            } else {
                for (i, s) in subs.iter_mut().enumerate() {
                    let (sx, sy) = sub_at(i);
                    *s = shade(sx + 0.5, sy + 0.5);
                }
                spread = (0..3)
                    .map(|k| subs.iter().map(|s| s[k]).fold(f64::NEG_INFINITY, f64::max) - subs.iter().map(|s| s[k]).fold(f64::INFINITY, f64::min))
                    .fold(f64::NEG_INFINITY, f64::max);
            }
            if o.supersample && spread > 40.0 {
                for (i, s) in subs.iter_mut().enumerate() {
                    let (sx, sy) = sub_at(i);
                    let mut acc = [0.0; 3];
                    for &(ox, oy) in taps {
                        let v = shade(sx + ox, sy + oy);
                        for k in 0..3 {
                            acc[k] += v[k];
                        }
                    }
                    *s = acc.map(|a| a / taps.len() as f64);
                }
            }
            let (ch, fg, bg) = fit(&subs, o.safe);
            if ch == ' ' && fg == base_rgb {
                continue;
            }
            out.push((r, c, Cell { ch, fg, bg }));
        }
    }
    out
}
