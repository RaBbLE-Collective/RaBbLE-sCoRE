//! `cargo run --release --offline -p rabble-face --example show -- [mini|COLSxROWS] [expr] [t]`: one frame as
//! truecolor ANSI on stdout (add `plain` as a last argument for glyphs only).
use rabble_face::{ansi, Detail, Expr, Face, FacePalette};
use rabble_theme::Palette;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let pal = FacePalette::from_palette(&Palette::discover().expect("palette"));
    let size = args.first().map(String::as_str).unwrap_or("mini");
    let (detail, cols, rows) = match size.split_once('x') {
        Some((c, r)) => (Detail::Full, c.parse().unwrap(), r.parse().unwrap()),
        None => (Detail::Mini, 24, 6),
    };
    let mut f = Face::new(cols, rows, pal, detail, false);
    f.expr = args.get(1).and_then(|e| Expr::from_name(e)).unwrap_or(Expr::Calm);
    let t = args.get(2).and_then(|t| t.parse().ok()).unwrap_or(1.0);
    let grid = f.frame(t, 0.0, (0.2, 0.1));
    if args.last().map(String::as_str) == Some("plain") {
        for row in grid {
            println!("{}", row.iter().map(|c| c.ch).collect::<String>());
        }
    } else {
        print!("{}", ansi(&grid, Some(pal.bg)));
    }
}
