//! The micro mark: two eyes drawn straight onto a 3-row cell grid (20 x 12 octant pixels), not a downscaled
//! face. It is the Matrix Builder's glyph (RaBbLE-NeBuLA-EyeStates.md) at this size: each eye is a slim white oval
//! crossing its own portal, a flat ellipse eight pixels wide with the eye two wide and centred in it, the left
//! portal along the top cell row and the right one cell row lower, the pair a cell apart. As in the face, the eye
//! starts in the portal's hollow, just inside its far edge, and is in front of the near edge (never cut by it).
//! A cell holds two colours, so the far edge and the eye share a cell as the portal colour over white, which needs
//! the eye to fill the rest of that cell (no void in it). Everywhere else a cell is one colour over the bg (the
//! terminal's own), so it reads on any theme. That is also what limits movement: gaze steps the eye a whole cell
//! sideways (it keeps filling its cells) and lengthens or shortens it along its portal; a blink retracts it, then
//! the portal closes, then both shut to a dash. `safe` (a terminal without octant glyphs, which draw only
//! quadrants) gets its own half-height grid, drawn the same way: merging the fine one would fill the hollows.
//! Pure: (blink, gaze, palette) -> cells. The Python twin ports `pixels` and `micro` as written.

use crate::{canvas::octant_table, Cell, FacePalette, Grid};

/// Cells the mark takes: four and a half per portal, a cell between them so no cell holds both rings.
pub const MICRO_COLS: usize = 10;
pub const MICRO_ROWS: usize = 3;
const W: usize = MICRO_COLS * 2; // octant pixels across
const H: usize = MICRO_ROWS * 4; // and down

pub const VOID: u8 = 0;
/// Left eye's portal (cyan) and the right's (magenta); both count as ring.
pub const RING: u8 = 1;
pub const WHITE: u8 = 2;
pub const RING_R: u8 = 3;

/// Pixel x where each eye's two pixels start: the right is the left mirrored about the grid's middle, so each sits
/// on a cell edge, centred in its portal.
const EYE_X: [usize; 2] = [4, 14];

/// One resolution's picture of the pair. Each portal is drawn from its own far edge (own row 0) to its near edge;
/// the left runs down the grid from `left_top`, the right is the same picture upside down with its far edge on
/// `right_base`. Each far edge ends a cell, so the black row below it and the eye never share a cell with it.
struct Geo {
    h: usize,
    /// Pixel x where each portal starts.
    slots: [usize; 2],
    portal: &'static [&'static str],
    left_top: usize,
    right_base: usize,
    /// The eye's first own row (one black row inside the far edge) and how many rows it runs.
    eye_y: usize,
    eye_rows: usize,
    /// The eye's rows when half shut.
    half_rows: usize,
}

/// 20 x 12: portal 8 x 4, left far edge on row 3 and right on row 8, eyes 6 rows with a black row inside the edge.
const FINE: Geo = Geo { h: H, slots: [1, 11], portal: &[".oooooo.", "o......o", "o......o", ".oooooo."], left_top: 3, right_base: 8, eye_y: 2, eye_rows: 6, half_rows: 3 };
/// 20 x 6, for quadrant glyphs: portal 8 x 3, eyes 3 rows.
const SAFE: Geo = Geo { h: H / 2, slots: [1, 11], portal: &[".oooooo.", "o......o", ".oooooo."], left_top: 1, right_base: 4, eye_y: 2, eye_rows: 3, half_rows: 1 };

fn is_ring(k: u8) -> bool {
    k == RING || k == RING_R
}

impl Geo {
    /// The grid row of own row `y` for eye `i`, if it is on the grid.
    fn row(&self, i: usize, y: i32) -> Option<usize> {
        let r = if i == 0 { self.left_top as i32 + y } else { self.right_base as i32 - y };
        (0..self.h as i32).contains(&r).then(|| r as usize)
    }
}

/// The pixels of one geometry, `[y][x]`, each VOID, RING, RING_R or WHITE.
/// `gaze` x steps each eye a cell (2 px) along its portal. y (+ = down) slides the left eye out of its portal and
/// the right eye into its own (it hangs the other way): the pill keeps its length, the far edge hides what has gone
/// behind it, and the row it leaves is black. An eye sinks until only its near-edge end shows and comes out as far
/// as the grid has room. `blink` 0..1 (1 = shut) retracts the white to half, then to nothing so the portal alone
/// stands, then both eyes shut to a dash in the hollow.
fn grid(g: &Geo, blink: f64, gaze: (f64, f64)) -> Vec<[u8; W]> {
    let blink = blink.clamp(0.0, 1.0);
    let (gx, gy) = (gaze.0.clamp(-1.0, 1.0), gaze.1.clamp(-1.0, 1.0));
    let step = if gx <= -0.4 { -2 } else if gx >= 0.4 { 2 } else { 0 };
    let near = g.portal.len() as i32 - 1;
    let last = g.eye_y as i32 + g.eye_rows as i32 - 1;
    // Both eyes move together and stop together. One goes out by `outs[m]` pixels while the other sinks m: out a
    // pixel at most (the
    // motion stays inside the portals), and in until only the near edge's end shows.
    let band = |y: i32| g.row(0, y).map(|r| r / (g.h / MICRO_ROWS));
    let clear = (near + 1..).find(|&y| band(y) != band(near)).unwrap_or(near + 1);
    let outs: Vec<i32> = (0..=(g.h as i32 - 1 - g.left_top as i32 - last).clamp(0, 1)).filter(|&o| !(g.eye_y as i32 + o > near && g.eye_y as i32 + o < clear)).collect();
    let steps = (outs.len() as i32 - 1).min(last - near).max(0);
    let down = (gy * steps as f64).round() as i32;
    let mut px = vec![[VOID; W]; g.h];
    for (i, (slot, eye_x)) in g.slots.into_iter().zip(EYE_X).enumerate() {
        let ring = if i == 0 { RING } else { RING_R };
        if blink >= 0.8 {
            let dash = if g.portal.len() >= 4 { 2 } else { 1 };
            for y in (g.portal.len() - dash) / 2..(g.portal.len() + dash) / 2 {
                for x in eye_x - 1..eye_x + 2 {
                    px[g.row(i, y as i32).unwrap()][x] = ring;
                }
            }
            continue;
        }
        let slide = if (i == 0) == (down >= 0) { outs[down.unsigned_abs() as usize] } else { -down.abs() };
        let n = if blink < 0.3 { g.eye_rows } else if blink < 0.55 { g.half_rows } else { 0 } as i32;
        let top = g.eye_y as i32 + slide;
        // the far edge hides whatever is behind it: only own rows 1 and up are seen
        let mut rows: Vec<i32> = (top..top + n).filter(|&y| y >= 1 && g.row(i, y).is_some()).collect();
        // a sliver that stops short of the near edge would leave white, a gap and the edge in one cell: it is gone
        if rows.last().is_some_and(|&y| y < near) {
            rows.clear();
        }
        let ex = (eye_x as i32 + step) as usize;
        for (y, row) in g.portal.iter().enumerate() {
            for (x, ch) in row.chars().enumerate() {
                if ch == 'o' && !(rows.contains(&(y as i32)) && (ex..ex + 2).contains(&(slot + x))) {
                    px[g.row(i, y as i32).unwrap()][slot + x] = ring;
                }
            }
        }
        for &y in &rows {
            for x in ex..ex + 2 {
                px[g.row(i, y).unwrap()][x] = WHITE;
            }
        }
    }
    px
}

/// The fine eye pixels, `[y][x]`.
pub fn pixels(blink: f64, gaze: (f64, f64)) -> [[u8; W]; H] {
    let mut out = [[VOID; W]; H];
    for (y, row) in grid(&FINE, blink, gaze).into_iter().enumerate() {
        out[y] = row;
    }
    out
}

/// The cells. A cell that meets both ring and white is the ring colour over white (the eye fills it; the edge is
/// drawn across it); otherwise the one colour it meets over the bg.
/// `safe`: quadrant glyphs only, from the half-height grid (each of its rows is two octant rows).
pub fn micro(pal: &FacePalette, blink: f64, gaze: (f64, f64), safe: bool) -> Grid {
    let px = grid(if safe { &SAFE } else { &FINE }, blink, gaze);
    let per = if safe { 2 } else { 4 }; // pixel rows a cell row spans
    (0..MICRO_ROWS).map(|cy| (0..MICRO_COLS).map(|cx| {
        let mut cell = [[VOID; 2]; 4];
        for r in 0..4 {
            for c in 0..2 {
                cell[r][c] = px[cy * per + if safe { r / 2 } else { r }][cx * 2 + c];
            }
        }
        let all = || cell.iter().flatten().copied();
        let ring = all().any(is_ring);
        let white = all().any(|k| k == WHITE);
        let ring_fg = if all().any(|k| k == RING) { pal.cyan } else { pal.magenta };
        let (fg, bg, lit): (_, _, fn(u8) -> bool) = match (ring, white) {
            (false, false) => (pal.bg, pal.bg, |_| false),
            (true, false) => (ring_fg, pal.bg, is_ring),
            (false, true) => (pal.text, pal.bg, |k| k == WHITE),
            (true, true) => (ring_fg, pal.text, is_ring),
        };
        let mut bits = 0usize;
        for r in 0..4 {
            for c in 0..2 {
                if lit(cell[r][c]) {
                    bits |= 1 << (r * 2 + c);
                }
            }
        }
        Cell { ch: octant_table()[bits], fg, bg }
    }).collect()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use rabble_theme::Palette;

    fn pal() -> FacePalette {
        FacePalette::from_palette(&Palette::parse("  bg: \"#0a0010\"\n  surface: \"#101018\"\n  text: \"#e8e6f0\"\n  muted: \"#6b6880\"\n  magenta: \"#ff2bd6\"\n  cyan: \"#00f5ff\"\n  violet: \"#8a4dff\"\n  pink: \"#ff7ab8\"\n"))
    }

    fn ascii(blink: f64, gaze: (f64, f64)) -> Vec<String> {
        pixels(blink, gaze).iter().map(|r| r.iter().map(|&b| match b { RING | RING_R => 'o', WHITE => 'W', _ => '.' }).collect()).collect()
    }

    fn count(blink: f64, k: u8) -> usize {
        pixels(blink, (0.0, 0.0)).iter().flatten().filter(|&&p| p == k).count()
    }

    #[test]
    fn neutral_pose_pins_the_pixels() {
        assert_eq!(ascii(0.0, (0.0, 0.0)), [
            "....................", "..............WW....", "..............WW....", "..oooooo......WW....", ".o......o.....WW....", ".o..WW..o...ooWWoo..", "..ooWWoo...o..WW..o.", "....WW.....o......o.", "....WW......oooooo..", "....WW..............", "....WW..............", "....................",
        ]);
    }

    #[test]
    fn colours_stay_with_their_eye_and_the_white_is_the_text_shade() {
        let (p, g) = (pal(), micro(&pal(), 0.0, (0.0, 0.0), false));
        let fgs = |r: usize, cols: std::ops::Range<usize>| g[r][cols].iter().filter(|c| c.ch != ' ').map(|c| c.fg).collect::<Vec<_>>();
        assert!(fgs(0, 0..3).contains(&p.cyan) && !fgs(0, 0..3).contains(&p.magenta), "top portal is the left eye's, cyan");
        assert!(fgs(1, 5..10).contains(&p.magenta) && !fgs(1, 5..10).contains(&p.cyan), "the lower portal is the right eye's, magenta");
        assert!(g.iter().flatten().any(|c| c.fg == p.text || c.bg == p.text), "the eye is white");
        assert!(g.iter().flatten().all(|c| [p.bg, p.text, p.cyan, p.magenta].contains(&c.fg) && (c.bg == p.bg || c.bg == p.text)), "no stray colours");
    }

    #[test]
    fn gaze_moves_the_eyes_and_a_blink_closes_them() {
        let calm = ascii(0.0, (0.0, 0.0));
        for g in [(1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)] {
            assert_ne!(calm, ascii(0.0, g), "gaze {g:?} moves something");
        }
        assert_eq!(ascii(0.0, (0.2, 0.0)), calm, "a small gaze does not step the eye");
        assert!(count(0.0, WHITE) > count(0.4, WHITE) && count(0.4, WHITE) > count(0.7, WHITE) && count(1.0, WHITE) == 0, "a blink closes the white");
        assert!(count(1.0, RING) > 0 && count(1.0, RING_R) > 0, "shut is a dash, never nothing");
    }

    fn ascii_of(g: &Geo, blink: f64, gaze: (f64, f64)) -> Vec<String> {
        grid(g, blink, gaze).iter().map(|r| r.iter().map(|&b| match b { RING | RING_R => 'o', WHITE => 'W', _ => '.' }).collect()).collect()
    }

    /// Every pose of both grids, every gaze and blink: both rings whole, an open eye at least fills its shared
    /// cell, and a cell holding ring and white has no void in it, so the white never leaks into the black and no
    /// arc is lost.
    #[test]
    fn every_pose_is_valid() {
        for (name, g) in [("fine", &FINE), ("safe", &SAFE)] {
            let n = |px: &Vec<[u8; W]>, f: fn(u8) -> bool| px.iter().flatten().filter(|&&k| f(k)).count();
            // the portal's pixels less the two the eye is in front of, per eye
            let portal: usize = g.portal.iter().map(|r| r.matches('o').count()).sum();
            let per = g.h / MICRO_ROWS;
            for gx in [-1.0, -0.5, 0.0, 0.5, 1.0] {
                for gy in [-1.0, -0.5, 0.0, 0.5, 1.0] {
                    for b in [0.0, 0.2, 0.29, 0.3, 0.4, 0.54, 0.55, 0.7, 0.79, 0.8, 1.0] {
                        let px = grid(g, b, (gx, gy));
                        let at = format!("{name} blink {b} gaze {:?}\n{}", (gx, gy), ascii_of(g, b, (gx, gy)).join("\n"));
                        for cy in 0..MICRO_ROWS {
                            for cx in 0..MICRO_COLS {
                                let cell: Vec<u8> = (0..per).flat_map(|r| (0..2).map(move |c| (r, c))).map(|(r, c)| px[cy * per + r][cx * 2 + c]).collect();
                                let (r, w) = (cell.iter().any(|&k| is_ring(k)), cell.contains(&WHITE));
                                assert!(!(r && w) || !cell.contains(&VOID), "ring and white share cell ({cx},{cy}) with a void in it: {at}");
                                assert!(!(cell.contains(&RING) && cell.contains(&RING_R)), "two rings in cell ({cx},{cy}): {at}");
                            }
                        }
                        if b < 0.3 {
                            // each eye hides two pixels in front of its near edge and cuts at most two more from the far edge when part way out
                            let rings = n(&px, is_ring);
                            assert!((2 * portal - 8..=2 * portal).contains(&rings), "a ring lost or gained pixels: {at}");
                            assert!(n(&px, |k| k == WHITE) >= 2 * 2 * g.half_rows, "an eye is shorter than its cell: {at}");
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn neutral_leaves_one_black_row_between_each_far_edge_and_its_pill() {
        let n = ascii(0.0, (0.0, 0.0));
        assert_eq!((&n[3], &n[4]), (&"..oooooo......WW....".to_string(), &".o......o.....WW....".to_string()), "left: edge whole, then black");
        assert_eq!(&n[7][11..19], "o......o", "right: black row inside the edge");
        assert_eq!(&n[8][12..18], "oooooo", "right far edge whole");
    }

    #[test]
    fn sliding_never_cuts_an_edge_and_the_pill_keeps_its_length_until_the_portal_hides_it() {
        let whites = |rows: &[String], x: usize| rows.iter().map(|r| r[x..x + 2].matches('W').count()).sum::<usize>();
        for gy in [-1.0, -0.66, -0.33, 0.0, 0.33, 0.66, 1.0] {
            let p = ascii(0.0, (0.0, gy));
            assert_eq!(&p[3][2..8], "oooooo", "left far edge stays whole at gaze {gy}");
            assert_eq!(&p[8][12..18].replace('W', "o"), "oooooo", "right far edge stays whole at gaze {gy}");
        }
        assert_eq!(whites(&ascii(0.0, (0.0, 0.33)), 4), 12, "out by a pixel keeps six rows");
        assert_eq!(whites(&ascii(0.0, (0.0, -1.0)), 4), 12, "sunk by a pixel the pill touches the far edge and keeps its six rows");
        assert_eq!(ascii(0.0, (0.0, -1.0))[4].as_bytes()[4], b'W', "the black row is used up");
    }

    #[test]
    fn the_pills_stop_together() {
        let (a, b) = (ascii(0.0, (0.0, 0.8)), ascii(0.0, (0.0, 1.0)));
        assert_eq!(a, b, "both pills have reached the end of their travel, neither goes on moving");
        assert_eq!(ascii(0.0, (0.0, -0.8)), ascii(0.0, (0.0, -1.0)));
    }

    #[test]
    fn neutral_safe_pose_pins_the_pixels() {
        assert_eq!(ascii_of(&SAFE, 0.0, (0.0, 0.0)), ["..............WW....", "..oooooo......WW....", ".o......o...ooWWoo..", "..ooWWoo...o......o.", "....WW......oooooo..", "....WW.............."]);
    }

    #[test]
    fn safe_mode_uses_quadrant_glyphs_only() {
        let g = micro(&pal(), 0.0, (0.0, 0.0), true);
        assert!(g.iter().flatten().all(|c| c.ch == ' ' || ('\u{2580}'..='\u{259F}').contains(&c.ch)), "{:?}", g.iter().map(|r| r.iter().map(|c| c.ch).collect::<String>()).collect::<Vec<_>>());
    }
}

