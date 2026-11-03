//! The micro mark: two eye pills drawn straight onto a 2-row cell grid (14 x 8 octant pixels), not a
//! downscaled face. No ring, portals, aura, mesh or anti-aliasing: at this size they only smear.
//! Cyan left eye, magenta right, void = the palette bg (the terminal's own), so it reads on any theme.
//! Pure: (blink, gaze, palette) -> cells. The Python twin ports `pixels` and `micro` as written.

use crate::{canvas::octant_table, Cell, FacePalette, Grid};

/// Cells the mark takes: three per eye and one empty between them.
pub const MICRO_COLS: usize = 7;
pub const MICRO_ROWS: usize = 2;
const W: usize = MICRO_COLS * 2; // octant pixels across
const H: usize = MICRO_ROWS * 4; // and down
const EYE_W: usize = 3;
const EYE_H: usize = 6;
/// Pixel x where each eye's 6-pixel slot (three cells) starts.
const SLOTS: [usize; 2] = [0, 8];

/// Row widths of a pill `h` pixels tall, 3 wide: rounded when tall enough, a flat dash when shut.
fn pill(h: usize) -> &'static [usize] {
    match h {
        6 => &[1, 3, 3, 3, 3, 1],
        4 => &[1, 3, 3, 1],
        2 => &[3, 3],
        _ => &[3],
    }
}

/// The eye pixels, `[y][x]`. `blink` 0..1 (1 = shut) squashes the pill about its middle; `gaze` x, y in -1..1
/// slides it inside its slot: 0..3 pixels across (1 at rest, 2 for the right eye), 0..2 down (1 at rest).
pub fn pixels(blink: f64, gaze: (f64, f64)) -> [[bool; W]; H] {
    let (gx, gy) = (gaze.0.clamp(-1.0, 1.0), gaze.1.clamp(-1.0, 1.0));
    let dx = (1.4 + gx * 1.6).round().clamp(0.0, 3.0) as usize;
    let dy = (1.0 + gy).round().clamp(0.0, 2.0) as usize;
    let blink = blink.clamp(0.0, 1.0);
    let h = if blink < 0.3 { EYE_H } else if blink < 0.55 { 4 } else if blink < 0.8 { 2 } else { 1 };
    let mut px = [[false; W]; H];
    for (i, slot) in SLOTS.into_iter().enumerate() {
        // the right eye rests a pixel further in, so the pair sits centred in the seven cells
        let dx = if i == 1 { (dx + 1).min(3) } else { dx };
        for (r, &w) in pill(h).iter().enumerate() {
            let y = dy + (EYE_H - h) / 2 + r;
            let x0 = slot + dx + (EYE_W - w) / 2;
            for x in x0..x0 + w {
                px[y][x] = true;
            }
        }
    }
    px
}

/// The cells. `safe`: quadrant glyphs only (each octant row pair OR-ed), for terminals without octants.
pub fn micro(pal: &FacePalette, blink: f64, gaze: (f64, f64), safe: bool) -> Grid {
    let px = pixels(blink, gaze);
    (0..MICRO_ROWS).map(|cy| (0..MICRO_COLS).map(|cx| {
        let on = |r: usize, c: usize| px[cy * 4 + r][cx * 2 + c];
        let mut bits = 0usize;
        for r in 0..4 {
            for c in 0..2 {
                let lit = if safe { on(r & !1, c) || on(r | 1, c) } else { on(r, c) };
                if lit {
                    bits |= 1 << (r * 2 + c);
                }
            }
        }
        // the eye owning this cell: left slot is columns 0..3, right 4..7 (column 3 is the gap)
        let fg = if cx < 3 { pal.cyan } else { pal.magenta };
        Cell { ch: octant_table()[bits], fg: if bits == 0 { pal.bg } else { fg }, bg: pal.bg }
    }).collect()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use rabble_theme::Palette;

    fn pal() -> FacePalette {
        FacePalette::from_palette(&Palette::parse("  bg: \"#0a0010\"\n  surface: \"#101018\"\n  text: \"#e8e6f0\"\n  muted: \"#6b6880\"\n  magenta: \"#ff2bd6\"\n  cyan: \"#00f5ff\"\n  violet: \"#8a4dff\"\n  pink: \"#ff7ab8\"\n"))
    }

    fn rows(g: &Grid) -> Vec<String> {
        g.iter().map(|r| r.iter().map(|c| c.ch).collect()).collect()
    }

    fn ascii(blink: f64, gaze: (f64, f64)) -> Vec<String> {
        pixels(blink, gaze).iter().map(|r| r.iter().map(|&b| if b { '#' } else { '.' }).collect()).collect()
    }

    #[test]
    fn neutral_gaze_pins_the_pixels_and_the_glyph_rows() {
        assert_eq!(ascii(0.0, (0.0, 0.0)), [
            "..............", "..#........#..", ".###......###.", ".###......###.", ".###......###.", ".###......###.", "..#........#..", "..............",
        ]);
        let g = micro(&pal(), 0.0, (0.0, 0.0), false);
        assert_eq!(rows(&g), ["▗\u{1CDDE}   \u{1CDE1}▖", "▝\u{1CD17}   \u{1CD26}▘"]);
        assert_eq!((g[0][1].fg, g[0][1].bg), (pal().cyan, pal().bg));
        assert_eq!(g[0][5].fg, pal().magenta);
        assert_eq!(g[0][3].ch, ' ', "the gap between the eyes is void");
    }

    #[test]
    fn gaze_shifts_the_eyes_and_a_blink_squashes_them() {
        let at = |g| ascii(0.0, g);
        assert_ne!(at((0.0, 0.0)), at((1.0, 0.0)));
        assert_ne!(at((0.0, 0.0)), at((-1.0, 0.0)));
        assert_ne!(at((0.0, 0.0)), at((0.0, 1.0)));
        assert_ne!(at((0.0, 0.0)), at((0.0, -1.0)));
        let lit = |b| pixels(b, (0.0, 0.0)).iter().flatten().filter(|&&p| p).count();
        assert!(lit(0.0) > lit(0.4) && lit(0.4) > lit(0.7) && lit(0.7) > lit(1.0) && lit(1.0) > 0);
        // every pose keeps each eye inside its own three cells, so the colours never mix
        for gx in [-1.0, 0.0, 1.0] {
            for gy in [-1.0, 0.0, 1.0] {
                let g = micro(&pal(), 0.0, (gx, gy), false);
                assert!(g.iter().all(|r| r[3].ch == ' '), "gap stays clear at {gx},{gy}");
            }
        }
    }

    #[test]
    fn safe_mode_uses_quadrant_glyphs_only() {
        let g = micro(&pal(), 0.0, (0.0, 0.0), true);
        assert!(g.iter().flatten().all(|c| c.ch == ' ' || ('\u{2580}'..='\u{259F}').contains(&c.ch)), "{:?}", rows(&g));
    }
}
