//! `cargo run --release --offline -p rabble-face --example bench`: per-layer cost at several sizes.
use rabble_face::{Detail, Face, FacePalette};
use rabble_theme::Palette;
use std::time::Instant;

fn main() {
    let pal = FacePalette::from_palette(&Palette::discover().expect("palette"));
    println!("{:>8} {:>12} {:>12} {:>12} {:>12}", "size", "eyes ms", "eyes rough", "bg ms", "assemble ms");
    for (c, r) in [(24, 6), (80, 24), (100, 30), (160, 45), (260, 70)] {
        let detail = if (c, r) == (24, 6) { Detail::Mini } else { Detail::Full };
        let f = Face::new(c, r, pal, detail, false);
        let time = |mut w: Box<dyn FnMut(usize)>| {
            let n = 60;
            let s = Instant::now();
            for i in 0..n {
                w(i);
            }
            s.elapsed().as_secs_f64() * 1000.0 / n as f64
        };
        if detail == Detail::Mini {
            let ms = time(Box::new(|i| { f.frame(i as f64 * 0.05, 0.0, (0.2, 0.1)); }));
            println!("{:>8} frame {ms:.3} ms", format!("{c}x{r}"));
            continue;
        }
        let eyes = time(Box::new(|i| { f.layer(i as f64 * 0.033, 0.0, ((i % 7) as f64 / 7.0, 0.1), None); }));
        let first = f.layer(0.0, 0.0, (0.0, 0.0), None);
        let rough = time(Box::new(|i| { f.layer(0.0, 0.0, (1.0 - (i % 2) as f64 * 2.0, 0.5), Some(&first)); }));
        let bg = time(Box::new(|i| { f.background(i as f64 * 0.125); }));
        let base = f.background(0.0);
        let asm = time(Box::new(|_| { f.assemble(&base, Some(&first)); }));
        println!("{:>8} {eyes:>12.3} {rough:>12.3} {bg:>12.3} {asm:>12.3}", format!("{c}x{r}"));
    }
}
