//! `cargo run --offline -p nebula_glyph --example moves -- swap|turn|boot [COLSxROWS] [infinity]`: the reduced move's
//! 8 Hz frames as plain glyphs (no colour), to read the design.
use nebula_glyph::{Detail, Driver, Face, FacePalette, Move};
use rabble_theme::Palette;

fn main() {
    let a: Vec<String> = std::env::args().skip(1).collect();
    let mv = match a.first().map(String::as_str) {
        Some("turn") => Move::Turn,
        Some("boot") => Move::Boot,
        _ => Move::PortalSwap,
    };
    let (cols, rows) = a.get(1).and_then(|s| s.split_once('x')).and_then(|(c, r)| Some((c.parse().ok()?, r.parse().ok()?))).unwrap_or((44, 12));
    let t = "  bg: \"#0a0a14\"\n  text: \"#e8e8f4\"\n  cyan: \"#18d8e8\"\n  magenta: \"#e818c8\"\n  violet: \"#8a4af0\"\n  pink: \"#f070b8\"\n  muted: \"#7a7a96\"\n";
    let mut face = Face::new(cols, rows, FacePalette::from_palette(&Palette::parse(t)), Detail::Full, true);
    face.infinity = a.iter().any(|s| s == "infinity");
    let mut d = Driver::new();
    d.play(mv, 0.0);
    let mut last = -1.0;
    for i in 0..=8 {
        let now = i as f64 / 8.0 * mv.seconds().max(0.5);
        d.tick(now, now);
        let p = d.move_frame().map_or(1.0, |(_, p)| p);
        if p == last {
            continue;
        }
        last = p;
        face.pose = Some(d.pose());
        println!("--- {} progress {:.3} (t={:.3}s)", mv.name(), p, now);
        for row in face.frame(0.0, 0.0, (0.0, 0.0)) {
            println!("{}", row.iter().map(|c| c.ch).collect::<String>().trim_end());
        }
    }
}
