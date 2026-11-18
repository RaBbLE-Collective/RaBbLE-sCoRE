//! The entity's signal (ui/face.py `degrade`, curves, `eye_block`): noise from the model's capability,
//! breaking from the session's stability. Pure apart from the running burst held in `DegradeState`.

use crate::canvas::{Cell, Grid};
use crate::face::FacePalette;
use crate::prng::SplitMix64;
use rabble_theme::{mix, Rgb};
use std::collections::HashMap;

pub const NOISE_CURVE: [(f64, f64); 5] = [(100.0, 0.0), (85.0, 0.0), (70.0, 0.06), (35.0, 0.38), (0.0, 1.0)];
pub const BREAK_CURVE: [(f64, f64); 5] = [(100.0, 0.0), (90.0, 0.0), (75.0, 0.12), (40.0, 0.50), (0.0, 1.0)];
const STATIC_CHARS: [char; 7] = ['░', '▒', '▓', '·', ':', '.', '\''];

/// Piecewise linear through points (x descending), clamped at both ends.
fn curve(points: &[(f64, f64)], x: f64) -> f64 {
    if x >= points[0].0 {
        return points[0].1;
    }
    for w in points.windows(2) {
        let ((x0, y0), (x1, y1)) = (w[0], w[1]);
        if x >= x1 {
            return y0 + (y1 - y0) * (x0 - x) / (x0 - x1);
        }
    }
    points[points.len() - 1].1
}

/// The model's general capability (0-100) -> 0..1 noise/resolution loss.
pub fn noise_level(capability: f64) -> f64 {
    curve(&NOISE_CURVE, capability)
}

/// The session's stability (0-100) -> 0..1 breaking.
pub fn break_level(stability: f64) -> f64 {
    curve(&BREAK_CURVE, stability)
}

/// Noise -> the eye renderer's resolution step: 1 fine, 2 coarse, 3 chunky.
pub fn eye_block(noise: f64) -> u32 {
    1 + (noise > 0.25) as u32 + (noise > 0.6) as u32
}

#[derive(Debug, Default)]
pub struct DegradeState {
    memo_key: Option<(u64, u64, i64)>,
    memo: HashMap<Rgb, Rgb>,
    burst: Option<Burst>,
}

#[derive(Debug)]
struct Burst {
    ticks: i64,
    shift: Vec<(usize, i64)>,
}

pub fn degrade(cells: &Grid, brk: f64, pal: &FacePalette, rng: &mut SplitMix64, state: &mut DegradeState, noise: f64) -> Grid {
    if (brk <= 0.0 && noise <= 0.0) || cells.is_empty() {
        return cells.clone();
    }
    let (bg, text, muted, magenta, cyan) = (pal.bg, pal.text, pal.muted, pal.magenta, pal.cyan);
    let (rows, cols) = (cells.len(), cells[0].len());
    // 1. signal loss
    let drain = noise * 0.7 + brk * 0.3;
    let dim = noise * 0.35 + brk * 0.2;
    let step = 1 + (noise * 40.0) as i64;
    let key = (drain.to_bits(), dim.to_bits(), step);
    if state.memo_key != Some(key) {
        state.memo_key = Some(key);
        state.memo.clear();
    }
    let mut out: Grid = Vec::with_capacity(rows);
    for row in cells {
        let mut new = Vec::with_capacity(cols);
        for cell in row {
            let d = match state.memo.get(&cell.fg) {
                Some(d) => *d,
                None => {
                    let fg = cell.fg;
                    let luma = (0.3 * fg.0 as f64 + 0.59 * fg.1 as f64 + 0.11 * fg.2 as f64).round_ties_even() as u8;
                    let mut d = mix(mix(fg, Rgb(luma, luma, luma), drain), bg, dim);
                    if step > 1 {
                        let q = |v: u8| ((v as f64 / step as f64).round_ties_even() as i64 * step).min(255) as u8;
                        d = Rgb(q(d.0), q(d.1), q(d.2));
                    }
                    if state.memo.len() < 4096 {
                        state.memo.insert(fg, d);
                    }
                    d
                }
            };
            new.push(Cell { ch: cell.ch, fg: d, bg: cell.bg });
        }
        out.push(new);
    }
    // 2. bursts
    if state.burst.is_none() && rng.random() < brk * 0.18 {
        let n = 1 + (brk * 2.5) as usize;
        let ticks = rng.randint(2, 5);
        let rows_idx: Vec<usize> = (0..rows).collect();
        let mut shift = Vec::new();
        for r in rng.sample(&rows_idx, rows.min(n)) {
            let sgn = *rng.choice(&[-1i64, 1]);
            shift.push((r, sgn * rng.randint(1, 2 + (brk * 4.0) as i64)));
        }
        state.burst = Some(Burst { ticks, shift });
    }
    if let Some(burst) = state.burst.as_mut() {
        for &(r, k) in &burst.shift {
            let mut row = out[r].clone();
            if k != 0 && (k.unsigned_abs() as usize) < cols {
                if k > 0 {
                    row.rotate_right(k as usize);
                } else {
                    row.rotate_left((-k) as usize);
                }
            }
            let ghost = if k > 0 { magenta } else { cyan };
            out[r] = row.into_iter().map(|c| Cell { ch: c.ch, fg: if c.ch != ' ' { mix(c.fg, ghost, 0.55) } else { c.fg }, bg: c.bg }).collect();
        }
        burst.ticks -= 1;
        if burst.ticks <= 0 {
            state.burst = None;
        }
    }
    // 3. dropout
    if brk > 0.6 && rng.random() < (brk - 0.6) * 0.5 {
        let r = rng.randrange(rows);
        out[r] = out[r].iter().map(|c| Cell { ch: ' ', fg: c.fg, bg: c.bg }).collect();
    }
    // 4. grain
    let density = noise * 0.12 + brk * 0.08;
    if density > 0.0 {
        for r in 0..rows {
            for c in 0..cols {
                if rng.random() < density {
                    let back = out[r][c].bg;
                    let grey = mix(muted, text, rng.random() * 0.5);
                    let ch = *rng.choice(&STATIC_CHARS);
                    out[r][c] = Cell { ch, fg: mix(grey, bg, rng.random() * 0.6), bg: back };
                }
            }
        }
    }
    out
}
