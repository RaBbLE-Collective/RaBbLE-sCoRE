//! The palette, the only source of colour. No hex lives in Rust code: it is read at startup from
//! `RaBbLE-Grimoire/RaBbLE-Agent/RaBbLE-Palette.md` (the YAML block of `name: "#rrggbb"` lines),
//! exactly as `ui/theme.py` does. Grimoire is `$RABBLE_GRIMOIRE`, else the sibling of RaBbLE-sCoRE.
//! Shared by the shell and the face so neither waits on the other.

use std::{collections::BTreeMap, fmt, path::{Path, PathBuf}};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Rgb(pub u8, pub u8, pub u8);

impl Rgb {
    pub fn parse(hex: &str) -> Option<Rgb> {
        let h = hex.strip_prefix('#')?;
        if h.len() != 6 || !h.is_ascii() {
            return None;
        }
        let c = |i: usize| u8::from_str_radix(&h[i..i + 2], 16).ok();
        Some(Rgb(c(0)?, c(2)?, c(4)?))
    }
}

impl fmt::Display for Rgb {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "#{:02x}{:02x}{:02x}", self.0, self.1, self.2)
    }
}

/// `ui/face.py mix`: t clamped to [0,1], each channel rounded **half to even** like Python's `round`
/// (Rust's `f64::round` rounds half away from zero, which would break golden parity on .5 ties).
pub fn mix(a: Rgb, b: Rgb, t: f64) -> Rgb {
    let t = t.clamp(0.0, 1.0);
    let ch = |x: u8, y: u8| (x as f64 + (y as f64 - x as f64) * t).round_ties_even() as u8;
    Rgb(ch(a.0, b.0), ch(a.1, b.1), ch(a.2, b.2))
}

#[derive(Debug, Clone, Default)]
pub struct Palette {
    colours: BTreeMap<String, Rgb>,
}

#[derive(Debug)]
pub enum PaletteError {
    Io(PathBuf, std::io::Error),
    Empty(PathBuf),
    Missing(&'static str),
}

impl fmt::Display for PaletteError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            PaletteError::Io(p, e) => write!(f, "cannot read palette {}: {e}", p.display()),
            PaletteError::Empty(p) => write!(f, "no colours found in {}", p.display()),
            PaletteError::Missing(n) => write!(f, "palette has no `{n}` colour"),
        }
    }
}
impl std::error::Error for PaletteError {}

/// Names the shell and face rely on; a palette without them is refused rather than guessed at.
const REQUIRED: [&str; 8] = ["bg", "surface", "text", "muted", "magenta", "cyan", "violet", "pink"];

impl Palette {
    /// Parse the text of RaBbLE-Palette.md: lines like `  cyan: "#00f5ff"` (keys are word characters).
    pub fn parse(text: &str) -> Palette {
        let mut colours = BTreeMap::new();
        for line in text.lines() {
            if !line.starts_with(char::is_whitespace) {
                continue;
            }
            let Some((key, rest)) = line.trim().split_once(':') else { continue };
            if key.is_empty() || !key.chars().all(|c| c.is_alphanumeric() || c == '_') {
                continue;
            }
            let rest = rest.trim();
            let Some(hex) = rest.strip_prefix('"').and_then(|r| r.get(..7)).filter(|_| rest.as_bytes().get(8) == Some(&b'"')) else { continue };
            if let Some(rgb) = Rgb::parse(hex) {
                colours.insert(key.to_owned(), rgb);
            }
        }
        Palette { colours }
    }

    pub fn load(path: &Path) -> Result<Palette, PaletteError> {
        let text = std::fs::read_to_string(path).map_err(|e| PaletteError::Io(path.to_owned(), e))?;
        let p = Palette::parse(&text);
        if p.colours.is_empty() {
            return Err(PaletteError::Empty(path.to_owned()));
        }
        for name in REQUIRED {
            if !p.colours.contains_key(name) {
                return Err(PaletteError::Missing(name));
            }
        }
        Ok(p)
    }

    /// `$RABBLE_GRIMOIRE`, else `<RaBbLE-sCoRE>/../RaBbLE-Grimoire` found by walking up from the executable
    /// and the working directory.
    pub fn grimoire_root() -> PathBuf {
        if let Some(env) = std::env::var_os("RABBLE_GRIMOIRE") {
            return PathBuf::from(env);
        }
        let starts = [std::env::current_exe().ok(), std::env::current_dir().ok()];
        for start in starts.into_iter().flatten() {
            for dir in start.ancestors() {
                let cand = dir.join("RaBbLE-Grimoire");
                if cand.join("RaBbLE-Agent").is_dir() {
                    return cand;
                }
                if dir.file_name().is_some_and(|n| n == "RaBbLE-sCoRE") {
                    return dir.parent().unwrap_or(dir).join("RaBbLE-Grimoire");
                }
            }
        }
        PathBuf::from("RaBbLE-Grimoire")
    }

    pub fn discover() -> Result<Palette, PaletteError> {
        Palette::load(&Palette::grimoire_root().join("RaBbLE-Agent").join("RaBbLE-Palette.md"))
    }

    pub fn get(&self, name: &str) -> Option<Rgb> {
        self.colours.get(name).copied()
    }

    /// For the required names (checked at load); anything else is optional, use `get`.
    pub fn req(&self, name: &str) -> Rgb {
        self.colours[name]
    }

    pub fn iter(&self) -> impl Iterator<Item = (&str, Rgb)> {
        self.colours.iter().map(|(k, v)| (k.as_str(), *v))
    }

    pub fn len(&self) -> usize {
        self.colours.len()
    }
    pub fn is_empty(&self) -> bool {
        self.colours.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_the_yaml_block_only() {
        let p = Palette::parse("# title\n| Role | Variable | Hex |\n```yaml\npalette:\n  cyan:    \"#00F5FF\"\n  bg: \"#0a0a0f\"\n  bad: \"#12345\"\n  note: \"text\"\n```\nnot_indented: \"#ffffff\"\n");
        assert_eq!(p.get("cyan"), Some(Rgb(0, 245, 255)));
        assert_eq!(p.get("bg"), Some(Rgb(10, 10, 15)));
        assert_eq!((p.get("bad"), p.get("note"), p.get("not_indented")), (None, None, None));
    }

    #[test]
    fn mix_rounds_half_to_even_like_python() {
        // python: round(0.5)=0, round(1.5)=2, round(2.5)=2
        assert_eq!(mix(Rgb(0, 1, 2), Rgb(1, 2, 3), 0.5), Rgb(0, 2, 2));
        assert_eq!(mix(Rgb(0, 0, 0), Rgb(255, 255, 255), 2.0), Rgb(255, 255, 255)); // t clamps
        assert_eq!(mix(Rgb(9, 9, 9), Rgb(200, 0, 50), -1.0), Rgb(9, 9, 9));
    }

    /// The real palette: every required name resolves, and the Display round-trips.
    #[test]
    fn the_real_palette_loads() {
        let p = Palette::discover().expect("palette (set RABBLE_GRIMOIRE if the Grimoire is elsewhere)");
        assert!(p.len() >= 13);
        for n in REQUIRED {
            let c = p.req(n);
            assert_eq!(Rgb::parse(&c.to_string()), Some(c));
        }
    }

    #[test]
    fn a_palette_missing_core_colours_is_refused() {
        let dir = std::env::temp_dir().join(format!("rabble-theme-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let f = dir.join("p.md");
        std::fs::write(&f, "  cyan: \"#00f5ff\"\n").unwrap();
        assert!(matches!(Palette::load(&f), Err(PaletteError::Missing("bg"))));
        std::fs::remove_dir_all(&dir).ok();
    }
}
