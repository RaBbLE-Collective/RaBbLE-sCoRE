//! Python `tests/test_ui_face.py` equivalents that are not golden dumps, plus the frame-rate gates.

use rabble_face::face::{Expr, FacePalette, Provider};
use rabble_face::{Detail, Face, Grid};
use rabble_theme::{Palette, Rgb};
use std::time::Instant;

fn pal() -> FacePalette {
    // the palette's own colours are in the golden fixture; any distinct set exercises the same code
    let p = Palette::parse(
        "  bg: \"#0a0010\"\n  text: \"#e8e6f0\"\n  cyan: \"#00f5ff\"\n  magenta: \"#ff2bd6\"\n  violet: \"#8a4dff\"\n  pink: \"#ff7ab8\"\n  muted: \"#6b6880\"\n",
    );
    FacePalette::from_palette(&p)
}

fn face(cols: usize, rows: usize, detail: Detail) -> Face {
    Face::new(cols, rows, pal(), detail, false)
}

fn diff(a: &Grid, b: &Grid) -> usize {
    a.iter().zip(b).map(|(x, y)| x.iter().zip(y).filter(|(p, q)| p != q).count()).sum()
}

#[test]
fn mini_shape() {
    let g = face(24, 6, Detail::Mini).frame(1.0, 0.0, (0.0, 0.0));
    assert!(g.len() == 6 && g.iter().all(|r| r.len() == 24));
}

/// test_compose_matches_frame: the layered path (cached background + eye layer) draws what frame() draws.
#[test]
fn compose_matches_frame() {
    for (cols, rows) in [(100, 30), (220, 60)] {
        let f = face(cols, rows, Detail::Full);
        for (t, gaze) in [(0.0, (0.0, 0.0)), (1.3, (0.6, -0.4)), (2.2, (-0.8, 0.3))] {
            let reference = f.frame(t, 0.0, gaze);
            let (got, _) = f.compose(t, 0.0, gaze, Some(&f.background(t)), None);
            assert!(diff(&reference, &got) <= cols * rows / 100, "{cols}x{rows} t={t}");
        }
    }
}

#[test]
fn provider_aura_tints_outside_the_eyes_only() {
    let plain = face(100, 30, Detail::Full);
    let mut tinted = face(100, 30, Detail::Full);
    tinted.set_provider(Some(Provider::Anthropic));
    let (a, b) = (plain.frame(0.0, 0.0, (0.0, 0.0)), tinted.frame(0.0, 0.0, (0.0, 0.0)));
    assert_ne!(a, b, "the provider tints the halo");
    let (ex, ey, ..) = plain.rig(0.0, 0.0, (0.0, 0.0), false).e_l;
    assert_eq!(a[(ey / 2.0) as usize][ex as usize], b[(ey / 2.0) as usize][ex as usize], "the eye's own cells carry no aura");
}

#[test]
fn safe_glyphs_stay_in_block_elements() {
    for (detail, cols, rows) in [(Detail::Mini, 24, 6), (Detail::Full, 100, 30)] {
        let g = Face::new(cols, rows, pal(), detail, true).frame(0.5, 0.0, (0.3, 0.1));
        let odd: Vec<char> = g.iter().flatten().map(|c| c.ch).filter(|&c| !(c == ' ' || ('\u{2580}'..='\u{259F}').contains(&c) || ('\u{2800}'..='\u{28FF}').contains(&c))).collect();
        assert!(odd.is_empty(), "{odd:?}");
    }
}

#[test]
fn the_layer_holds_still_and_goes_rough_while_the_eyes_travel() {
    let f = face(100, 30, Detail::Full);
    let l1 = f.layer(0.0, 0.0, (0.0, 0.0), None);
    assert!(!l1.rough, "the first layer is clean");
    let l2 = f.layer(0.0, 0.0, (0.0, 0.0), Some(&l1));
    assert!(std::sync::Arc::ptr_eq(&l1, &l2), "a still rig returns the last layer as is");
    let l3 = f.layer(0.0, 0.0, (1.0, 1.0), Some(&l1));
    assert!(l3.rough, "a big move skips supersampling");
    let l4 = f.layer(0.0, 0.0, (1.0, 1.0), Some(&l3));
    assert!(!l4.rough, "the first still call repaints clean");
}

#[test]
fn expressions_all_draw() {
    for e in Expr::ALL {
        let mut f = face(60, 20, Detail::Full);
        f.expr = e;
        let g = f.frame(0.4, 0.0, (0.0, 0.0));
        assert!(g.iter().flatten().any(|c| c.bg != Rgb(0, 0, 0)));
    }
}

fn fps(mut work: impl FnMut(usize), secs: f64) -> f64 {
    work(0); // warm up
    let (start, mut n) = (Instant::now(), 0usize);
    while start.elapsed().as_secs_f64() < secs {
        n += 1;
        work(n);
    }
    n as f64 / start.elapsed().as_secs_f64()
}

/// T3: eyes >= 30 fps and background >= 15 fps at 260x70. Release builds only (the budget is for the
/// shipped binary); the margins are large (see `cargo run --release --example bench`).
#[cfg(not(debug_assertions))]
#[test]
fn frame_rate_gates_at_260x70() {
    let f = face(260, 70, Detail::Full);
    let eyes = fps(|i| { f.layer(i as f64 * 0.033, 0.0, ((i % 7) as f64 / 7.0, 0.1), None); }, 1.0);
    let bg = fps(|i| { f.background(i as f64 * 0.125); }, 1.0);
    eprintln!("260x70: eyes {eyes:.0} fps, background {bg:.0} fps");
    assert!(eyes >= 30.0, "eyes {eyes}");
    assert!(bg >= 15.0, "background {bg}");
}
