//! `cargo run --release --offline -p rabble-face --example export -- micro|mini|COLSxROWS [safe] [aspect=H/W] [label=TEXT]`: the mark as truecolor
//! ANSI on stdout with the terminal's own background, for surfaces outside the TUI (the fastfetch logo reads this;
//! RaBbLE-OS spells/fastfetch-face.sh). Gaze centred, eyes open. `safe` = quadrant glyphs for fonts without octants.
//! `aspect=` is the terminal cell's height over width (default 2.0; Mark's kitty is about 2.3), which makes the full face's sphere round.
//! `label=TEXT` adds a blank row and TEXT centred beneath the mark, coloured cyan, violet, pink, magenta along its length.
use rabble_face::{ansi, micro, Detail, Expr, Face, FacePalette};
use rabble_theme::Palette;

fn main() {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let aspect: f64 = args.iter().position(|a| a.starts_with("aspect=")).map(|i| args.remove(i)["aspect=".len()..].parse().expect("aspect=NUMBER")).unwrap_or(2.0);
    let label = args.iter().position(|a| a.starts_with("label=")).map(|i| args.remove(i)["label=".len()..].to_string());
    let pal = FacePalette::from_palette(&Palette::discover().expect("palette"));
    let safe = args.last().map(String::as_str) == Some("safe");
    let size = args.first().map(String::as_str).unwrap_or("micro");
    let grid = match size {
        "micro" => micro::micro(&pal, 0.0, (0.0, 0.0), safe),
        "mini" => Face::new(24, 6, pal, Detail::Mini, safe).frame(1.0, 0.0, (0.0, 0.0)),
        wh => {
            let (c, r) = wh.split_once('x').expect("micro|mini|COLSxROWS");
            let mut f = Face::with_aspect(c.parse().unwrap(), r.parse().unwrap(), pal, Detail::Full, safe, aspect);
            f.expr = Expr::Calm;
            f.frame(1.0, 0.0, (0.0, 0.0))
        }
    };
    let width = grid.first().map_or(0, |r| r.len());
    print!("{}", ansi(&grid, Some(pal.bg)));
    if let Some(text) = label {
        let ramp = [pal.cyan, pal.violet, pal.pink, pal.magenta];
        let chars: Vec<char> = text.chars().collect();
        let pad = width.saturating_sub(chars.len()) / 2;
        println!();
        print!("{}", " ".repeat(pad));
        for (i, ch) in chars.iter().enumerate() {
            let c = ramp[i * ramp.len() / chars.len().max(1)];
            print!("\x1b[1;38;2;{};{};{}m{}", c.0, c.1, c.2, ch);
        }
        println!("\x1b[0m");
    }
}
