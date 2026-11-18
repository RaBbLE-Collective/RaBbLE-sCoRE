//! `Canvas`: half-block pixels + braille dots on one cell grid, or octant mode (ui/face.py `Canvas`).

use rabble_theme::Rgb;
use std::sync::OnceLock;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Cell {
    pub ch: char,
    pub fg: Rgb,
    pub bg: Rgb,
}

/// Rows of cells.
pub type Grid = Vec<Vec<Cell>>;

const BRAILLE: [[u32; 2]; 4] = [[0x01, 0x08], [0x02, 0x10], [0x04, 0x20], [0x40, 0x80]]; // [row][col]

/// Bit pattern (bit i = octant i+1; octants run 1 2 / 3 4 / 5 6 / 7 8) -> glyph. Unicode 16's BLOCK OCTANT
/// run (U+1CD00..) is in ascending bit order and skips the 26 patterns older block glyphs already draw.
pub fn octant_table() -> &'static [char; 256] {
    static T: OnceLock<[char; 256]> = OnceLock::new();
    T.get_or_init(|| {
        let older: [(&[u8], u32); 26] = [
            (&[], 0x20), (&[1], 0x1CEA8), (&[2], 0x1CEAB), (&[1, 2], 0x1FB82), (&[1, 3], 0x2598), (&[2, 4], 0x259D),
            (&[1, 2, 3, 4], 0x2580), (&[3, 5], 0x1FBE6), (&[4, 6], 0x1FBE7), (&[1, 2, 3, 4, 5, 6], 0x1FB85),
            (&[7], 0x1CEA3), (&[5, 7], 0x2596), (&[1, 3, 5, 7], 0x258C), (&[2, 4, 5, 7], 0x259E),
            (&[1, 2, 3, 4, 5, 7], 0x259B), (&[8], 0x1CEA0), (&[6, 8], 0x2597), (&[1, 3, 6, 8], 0x259A),
            (&[2, 4, 6, 8], 0x2590), (&[1, 2, 3, 4, 6, 8], 0x259C), (&[7, 8], 0x2582), (&[5, 6, 7, 8], 0x2584),
            (&[1, 3, 5, 6, 7, 8], 0x2599), (&[2, 4, 5, 6, 7, 8], 0x259F), (&[3, 4, 5, 6, 7, 8], 0x2586),
            (&[1, 2, 3, 4, 5, 6, 7, 8], 0x2588),
        ];
        let mut t = ['\0'; 256];
        for (octs, cp) in older {
            let pat: usize = octs.iter().map(|o| 1usize << (o - 1)).sum();
            t[pat] = char::from_u32(cp).unwrap();
        }
        let mut next = 0x1CD00u32;
        for slot in t.iter_mut() {
            if *slot == '\0' {
                *slot = char::from_u32(next).unwrap();
                next += 1;
            }
        }
        assert_eq!(next, 0x1CDE6);
        t
    })
}

fn dist(a: Rgb, b: Rgb) -> i64 {
    let d = |x: u8, y: u8| (x as i64 - y as i64).pow(2);
    d(a.0, b.0) + d(a.1, b.1) + d(a.2, b.2)
}

/// The commonest colour, ties to the first seen (Python `max(dict.fromkeys(cols), key=cols.count)`).
fn mode(cols: &[Rgb]) -> Rgb {
    let mut best = (0usize, cols[0]);
    for (i, &c) in cols.iter().enumerate() {
        if cols[..i].contains(&c) {
            continue;
        }
        let n = cols.iter().filter(|&&x| x == c).count();
        if n > best.0 {
            best = (n, c);
        }
    }
    best.1
}

pub struct Canvas {
    pub cols: usize,
    pub rows: usize,
    pub octant: bool,
    pub safe: bool,
    pub w: usize,
    pub h: usize,
    pub bg: Vec<Vec<Rgb>>,
    fill: Vec<Option<Rgb>>,
    dots: Vec<Option<(Rgb, f64)>>,
    dot_w: usize,
    pub over: Vec<(usize, usize, Cell)>,
}

impl Canvas {
    pub fn new(cols: usize, rows: usize, bg: Rgb, octant: bool, safe: bool) -> Canvas {
        let (w, h) = if octant { (cols * 2, rows * 4) } else { (cols, rows * 2) };
        let k = if octant { 1 } else { 2 };
        Canvas {
            cols, rows, octant, safe, w, h,
            bg: vec![vec![bg; cols]; rows],
            fill: vec![None; w * h],
            dots: vec![None; w * k * h * k],
            dot_w: w * k,
            over: Vec::new(),
        }
    }

    pub fn glow(&mut self, f: impl Fn(f64, f64, Rgb) -> Rgb) {
        let (sx, sy) = (self.w as f64 / self.cols as f64, self.h as f64 / self.rows as f64);
        for r in 0..self.rows {
            for c in 0..self.cols {
                self.bg[r][c] = f((c as f64 + 0.5) * sx, (r as f64 + 0.5) * sy, self.bg[r][c]);
            }
        }
    }

    pub fn px(&mut self, x: f64, y: f64, color: Rgb) {
        let (x, y) = (x.floor(), y.floor());
        if x >= 0.0 && y >= 0.0 && (x as usize) < self.w && (y as usize) < self.h {
            self.fill[y as usize * self.w + x as usize] = Some(color);
        }
    }

    /// x, y in pixel units; lands on the 2x braille grid (or the pixel grid in octant mode).
    pub fn dot(&mut self, x: f64, y: f64, color: Rgb, weight: f64) {
        let k = if self.octant { 1.0 } else { 2.0 };
        let (dx, dy) = ((x * k).floor(), (y * k).floor());
        if dx >= 0.0 && dy >= 0.0 && (dx as usize) < self.dot_w && (dy as usize) < self.dots.len() / self.dot_w {
            let slot = &mut self.dots[dy as usize * self.dot_w + dx as usize];
            if slot.map_or(true, |o| weight >= o.1) {
                *slot = Some((color, weight));
            }
        }
    }

    pub fn line(&mut self, x0: f64, y0: f64, x1: f64, y1: f64, color: Rgb, weight: f64) {
        let n = ((x1 - x0).hypot(y1 - y0) * 2.2) as i64;
        let n = n.max(1);
        for i in 0..=n {
            let t = i as f64 / n as f64;
            self.dot(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, color, weight);
        }
    }

    fn fill_at(&self, x: usize, y: usize) -> Option<Rgb> {
        self.fill[y * self.w + x]
    }
    fn dot_at(&self, x: usize, y: usize) -> Option<(Rgb, f64)> {
        self.dots[y * self.dot_w + x]
    }

    fn cell(&self, r: usize, c: usize) -> Cell {
        let bg = self.bg[r][c];
        if self.octant {
            return self.octant_cell(r, c, bg);
        }
        let (top, bot) = (self.fill_at(c, r * 2), self.fill_at(c, r * 2 + 1));
        if top.is_some() || bot.is_some() {
            return Cell { ch: '▀', fg: top.unwrap_or(bg), bg: bot.unwrap_or(bg) };
        }
        let (mut bits, mut best): (u32, Option<(Rgb, f64)>) = (0, None);
        for (yy, row) in BRAILLE.iter().enumerate() {
            for (xx, bit) in row.iter().enumerate() {
                if let Some(d) = self.dot_at(c * 2 + xx, r * 4 + yy) {
                    bits |= bit;
                    if best.map_or(true, |b| d.1 > b.1) {
                        best = Some(d);
                    }
                }
            }
        }
        Cell {
            ch: if bits != 0 { char::from_u32(0x2800 + bits).unwrap() } else { ' ' },
            fg: best.map_or(bg, |b| b.0),
            bg,
        }
    }

    /// Eight sub-pixels, two colours: split them around the most distant pair.
    fn octant_cell(&self, r: usize, c: usize, bg: Rgb) -> Cell {
        let mut subs = [bg; 8];
        for yy in 0..4 {
            for xx in 0..2 {
                let (x, y) = (c * 2 + xx, r * 4 + yy);
                subs[yy * 2 + xx] = self.fill_at(x, y).or(self.dot_at(x, y).map(|d| d.0)).unwrap_or(bg);
            }
        }
        let mut uniq: Vec<Rgb> = Vec::with_capacity(8);
        for s in subs {
            if !uniq.contains(&s) {
                uniq.push(s);
            }
        }
        if uniq.len() == 1 {
            return Cell { ch: ' ', fg: bg, bg: uniq[0] };
        }
        let (mut a, mut b, mut best) = (uniq[0], uniq[1], -1i64);
        for i in 0..uniq.len() {
            for j in i + 1..uniq.len() {
                let d = dist(uniq[i], uniq[j]);
                if d > best {
                    best = d;
                    a = uniq[i];
                    b = uniq[j];
                }
            }
        }
        if b == bg {
            std::mem::swap(&mut a, &mut b);
        }
        if a != bg && dist(a, bg) < dist(b, bg) {
            std::mem::swap(&mut a, &mut b);
        }
        let mut ins = [false; 8];
        for (i, s) in subs.iter().enumerate() {
            ins[i] = dist(*s, b) < dist(*s, a);
        }
        let on: Vec<Rgb> = subs.iter().zip(ins).filter(|(_, i)| *i).map(|(s, _)| *s).collect();
        let off: Vec<Rgb> = subs.iter().zip(ins).filter(|(_, i)| !*i).map(|(s, _)| *s).collect();
        let fg = mode(&on);
        let back = if !off.is_empty() { mode(&off) } else { bg };
        if self.safe {
            for a in [0, 1, 4, 5] {
                let v = ins[a] || ins[a + 2];
                ins[a] = v;
                ins[a + 2] = v;
            }
        }
        let bits: usize = ins.iter().enumerate().map(|(i, &on)| (on as usize) << i).sum();
        Cell { ch: octant_table()[bits], fg, bg: back }
    }

    /// The (glyph, fg, bg) grid.
    pub fn cells(&self) -> Grid {
        let mut g: Grid = (0..self.rows).map(|r| (0..self.cols).map(|c| self.cell(r, c)).collect()).collect();
        for &(r, c, cell) in &self.over {
            if r < self.rows && c < self.cols {
                g[r][c] = cell;
            }
        }
        g
    }
}

/// `Canvas.ansi`-like text for previews: truecolor, cells on `base_bg` use the default background.
pub fn ansi(grid: &Grid, base_bg: Option<Rgb>) -> String {
    let mut s = String::new();
    for row in grid {
        for cell in row {
            let back = if Some(cell.bg) == base_bg { "\x1b[49m".to_string() } else { format!("\x1b[48;2;{};{};{}m", cell.bg.0, cell.bg.1, cell.bg.2) };
            s.push_str(&format!("\x1b[38;2;{};{};{}m{}{}", cell.fg.0, cell.fg.1, cell.fg.2, back, cell.ch));
        }
        s.push_str("\x1b[0m\n");
    }
    s
}
