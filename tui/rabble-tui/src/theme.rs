//! Styles and glyphs for the shell. Colour comes only from `rabble_theme::Palette` (no hex here);
//! `NO_COLOR` turns every colour off and keeps the modifiers; `TERM_PROGRAM=vscode` (whose
//! terminal draws some box and block glyphs badly) gets plain safe glyphs.

use rabble_theme::{Palette, Rgb};
use ratatui::style::{Color, Modifier, Style};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Glyphs {
    pub rule: char,
    pub bullet: &'static str,
    pub prompt: &'static str,
    pub arrow: &'static str,
    pub ellipsis: &'static str,
    pub open: &'static str,
    pub shut: &'static str,
    pub quote: &'static str,
    pub corner: [char; 4], // tl tr bl br
    pub vert: char,
    pub bar: [char; 2], // filled, empty
}

impl Glyphs {
    pub const FANCY: Glyphs = Glyphs { rule: '─', bullet: "•", prompt: "›", arrow: "→", ellipsis: "…", open: "▾", shut: "▸", quote: "▎", corner: ['╭', '╮', '╰', '╯'], vert: '│', bar: ['▰', '▱'] };
    pub const SAFE: Glyphs = Glyphs { rule: '-', bullet: "*", prompt: ">", arrow: "->", ellipsis: "...", open: "v", shut: ">", quote: "|", corner: ['+', '+', '+', '+'], vert: '|', bar: ['#', '-'] };
    pub fn for_env(term_program: Option<&str>) -> Glyphs {
        if term_program == Some("vscode") { Glyphs::SAFE } else { Glyphs::FANCY }
    }
}

#[derive(Debug, Clone)]
pub struct Theme {
    pub color: bool,
    pub glyphs: Glyphs,
    pub text: Style,
    pub muted: Style,
    pub accent: Style,    // primary identity colour
    pub secondary: Style, // cyan
    pub tertiary: Style,  // violet
    pub error: Style,
    pub ok: Style,
    pub warn: Style,
    pub code: Style,
    pub user: Style,
    pub heading: Style,
    pub border: Style,
    pub modal: Style,
    pub select: Style,
    organs: Vec<Style>,
    /// The palette itself, for the face (it draws cell by cell); `None` without one.
    pub pal: Option<Palette>,
    /// cyan and magenta, the poles of the flowing divider and input border; `None` without colour.
    flow: Option<(Rgb, Rgb)>,
}

/// Full cyan -> magenta -> cyan sweeps across the width, and how many sweeps per second it drifts.
pub const FLOW_CYCLES: f64 = 1.5;
pub const FLOW_SPEED: f64 = 0.12;

fn c(r: Rgb) -> Color {
    Color::Rgb(r.0, r.1, r.2)
}

/// A palette-derived colour for cells the face paints (the only other place that builds `Color::Rgb`).
pub fn rgb(r: Rgb) -> Color {
    c(r)
}

impl Theme {
    /// Colours from the palette, honouring NO_COLOR and TERM_PROGRAM.
    pub fn new(p: Option<&Palette>, no_color: bool, glyphs: Glyphs) -> Theme {
        let colour = p.is_some() && !no_color;
        let fg = |name: &str| match p.and_then(|p| p.get(name)) {
            Some(r) if colour => Style::new().fg(c(r)),
            _ => Style::new(),
        };
        let both = |f: &str, b: &str| match (p.and_then(|p| p.get(f)), p.and_then(|p| p.get(b))) {
            (Some(f), Some(b)) if colour => Style::new().fg(c(f)).bg(c(b)),
            _ => Style::new(),
        };
        let bold = Modifier::BOLD;
        let mut t = Theme {
            color: colour,
            glyphs,
            text: fg("text"),
            muted: fg("muted"),
            accent: fg("magenta"),
            secondary: fg("cyan"),
            tertiary: fg("violet"),
            error: fg("red").add_modifier(bold),
            ok: fg("green"),
            warn: fg("yellow"),
            code: both("cyan", "surface"),
            user: fg("pink"),
            heading: fg("cyan").add_modifier(bold),
            border: fg("border"),
            modal: both("text", "raised"),
            select: Style::new().add_modifier(Modifier::REVERSED),
            organs: ["cyan", "violet", "pink", "green", "yellow"].iter().map(|n| fg(n)).collect(),
            pal: p.cloned(),
            flow: match (p.and_then(|p| p.get("cyan")), p.and_then(|p| p.get("magenta"))) {
                (Some(a), Some(b)) if colour => Some((a, b)),
                _ => None,
            },
        };
        if !colour {
            // without colour, errors and headings still need to stand apart
            t.error = Style::new().add_modifier(bold | Modifier::UNDERLINED);
            t.heading = Style::new().add_modifier(bold);
            t.code = Style::new().add_modifier(Modifier::DIM);
            t.muted = Style::new().add_modifier(Modifier::DIM);
            t.user = Style::new().add_modifier(bold);
        }
        t
    }

    pub fn from_env() -> Theme {
        let p = Palette::discover().ok();
        let no_color = std::env::var_os("NO_COLOR").is_some_and(|v| !v.is_empty());
        Theme::new(p.as_ref(), no_color, Glyphs::for_env(std::env::var("TERM_PROGRAM").ok().as_deref()))
    }

    /// Foreground for column `x` of `width` on the flowing cyan -> magenta -> cyan line (a cosine bounce, so no
    /// seam), drifting with `phase` (sweeps). `fallback` when there is no colour. Mirrors ui/widgets/divider.py.
    pub fn flow(&self, x: usize, width: usize, phase: f64, fallback: Style) -> Style {
        match self.flow {
            Some((a, b)) => {
                let w = 0.5 - 0.5 * (std::f64::consts::TAU * (x as f64 / width.max(1) as f64 * FLOW_CYCLES - phase)).cos();
                Style::new().fg(c(rabble_theme::mix(a, b, w)))
            }
            None => fallback,
        }
    }

    /// Whether the flowing lines move (colour on and a cyan/magenta pair in the palette).
    pub fn animates(&self) -> bool {
        self.flow.is_some()
    }

    /// Bold per-character blend `a` -> `b` of two palette names (the wordmark); `None` without colour.
    pub fn blend(&self, from: &str, to: &str, i: usize, n: usize) -> Option<Style> {
        let (a, b) = (self.pal.as_ref()?.get(from)?, self.pal.as_ref()?.get(to)?);
        self.flow?;
        Some(Style::new().fg(c(rabble_theme::mix(a, b, i as f64 / n.max(1) as f64))).add_modifier(Modifier::BOLD))
    }

    /// The lead ("rabble") reads as plain text; each organ gets a stable accent from the palette.
    pub fn organ(&self, organ: &str) -> Style {
        if organ == "rabble" || organ.is_empty() {
            return self.text;
        }
        let h = organ.bytes().fold(7usize, |a, b| a.wrapping_mul(31).wrapping_add(b as usize));
        self.organs[h % self.organs.len()]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pal() -> Palette {
        Palette::parse("  bg: \"#000001\"\n  surface: \"#000002\"\n  text: \"#000003\"\n  muted: \"#000004\"\n  magenta: \"#000005\"\n  cyan: \"#000006\"\n  violet: \"#000007\"\n  pink: \"#000008\"\n")
    }

    #[test]
    fn colour_comes_from_the_palette() {
        let t = Theme::new(Some(&pal()), false, Glyphs::FANCY);
        assert_eq!(t.accent.fg, Some(Color::Rgb(0, 0, 5)));
        assert!(t.color);
    }

    #[test]
    fn no_color_drops_every_colour_but_keeps_emphasis() {
        let t = Theme::new(Some(&pal()), true, Glyphs::FANCY);
        for s in [t.text, t.muted, t.accent, t.secondary, t.error, t.code, t.heading, t.user, t.modal, t.organ("gobble")] {
            assert_eq!((s.fg, s.bg), (None, None));
        }
        assert!(t.error.add_modifier.contains(Modifier::BOLD | Modifier::UNDERLINED));
    }

    #[test]
    fn a_missing_palette_degrades_to_plain() {
        let t = Theme::new(None, false, Glyphs::FANCY);
        assert!(!t.color && t.accent.fg.is_none());
    }

    #[test]
    fn vscode_gets_safe_glyphs() {
        assert_eq!(Glyphs::for_env(Some("vscode")), Glyphs::SAFE);
        assert_eq!(Glyphs::for_env(Some("kitty")), Glyphs::FANCY);
        assert!(Glyphs::SAFE.rule.is_ascii() && Glyphs::SAFE.corner.iter().all(|c| c.is_ascii()));
    }

    fn rs_files(dir: &std::path::Path, out: &mut Vec<std::path::PathBuf>) {
        for e in std::fs::read_dir(dir).unwrap_or_else(|e| panic!("{}: {e}", dir.display())) {
            let p = e.unwrap().path();
            if p.is_dir() {
                rs_files(&p, out);
            } else if p.extension().is_some_and(|x| x == "rs") {
                out.push(p);
            }
        }
    }

    /// Every file under src/ (a missing one fails the walk): no `#rrggbb` anywhere in non-test
    /// code, and `Color::Rgb(` only in theme.rs, which converts palette entries.
    #[test]
    fn no_hex_or_rgb_literals_anywhere_in_src() {
        let re_hex = |s: &str| s.as_bytes().windows(7).any(|w| w[0] == b'#' && w[1..].iter().all(u8::is_ascii_hexdigit));
        let mut files = Vec::new();
        rs_files(&std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src"), &mut files);
        assert!(files.len() >= 15, "walked {} files", files.len());
        for p in files {
            let src = std::fs::read_to_string(&p).unwrap();
            let src = src.split("#[cfg(test)]").next().unwrap();
            let name = p.file_name().unwrap().to_string_lossy().into_owned();
            assert!(!re_hex(src), "hex literal in {name}");
            assert!(name == "theme.rs" || !src.contains("Color::Rgb("), "Color::Rgb( in {name}");
            assert!(name == "theme.rs" || !src.contains("Color::Indexed("), "Color::Indexed( in {name}");
        }
    }
}
