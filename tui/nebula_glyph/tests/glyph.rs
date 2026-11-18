//! NeBuLA glyph: vocabulary (canonical names, alias table) and the reduced moves (portal swap, turn, boot).

use nebula_glyph::face::{Expr, Face, FacePalette, Pose, POR_OFF, POR_RX, POR_RY};
use nebula_glyph::moves::{self, Move, STEP_HZ};
use nebula_glyph::vocab::{Beat, Cue, Mood, State, ALIASES};
use nebula_glyph::{ansi, Detail, Driver};
use rabble_theme::Palette;

fn pal() -> FacePalette {
    let t = "  bg: \"#0a0a14\"\n  text: \"#e8e8f4\"\n  cyan: \"#18d8e8\"\n  magenta: \"#e818c8\"\n  violet: \"#8a4af0\"\n  pink: \"#f070b8\"\n  muted: \"#7a7a96\"\n";
    FacePalette::from_palette(&Palette::parse(t))
}

// ---- vocabulary ----

#[test]
fn private_names_resolve_through_the_alias_table() {
    for (private, canon) in ALIASES {
        let b = Beat::parse(private).unwrap_or_else(|| panic!("{private}"));
        assert_eq!(b.id(), canon, "{private}");
    }
    assert_eq!(Beat::from_name("typing"), Beat::LISTENING);
    assert_eq!(Beat::from_name("streaming"), Beat::SPEAKING);
    assert_eq!(Beat::from_name("working"), Beat::THINKING);
    assert_eq!(Beat::from_name("error"), Beat::ERROR);
    assert_eq!(Beat::from_name("denied"), Beat::DENIED);
    assert_eq!(Beat::from_name("nonsense"), Beat::IDLE);
    assert_eq!(Beat::parse("nonsense"), None);
}

#[test]
fn canonical_names_parse_as_themselves() {
    for s in State::ALL {
        assert_eq!(Beat::parse(s.name()), Some(Beat::State(s)));
        assert_eq!(Beat::parse(&format!("state.{}", s.name())), Some(Beat::State(s)));
    }
    for c in Cue::ALL {
        assert_eq!(Beat::parse(&format!("cue.{}", c.name())), Some(Beat::Cue(c)));
    }
    assert_eq!(Mood::ALL.map(|m| m.name()), ["ponder", "process", "insight"]);
    for e in [Expr::Calm, Expr::Joy, Expr::Sad, Expr::Angry] {
        assert!(["calm", "joy", "sad", "angry"].contains(&e.name()));
    }
}

/// The alias table is a copy of the registry's; fail when the registry moves on (skipped if the Grimoire is absent).
#[test]
fn alias_table_matches_the_registry() {
    let p = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../RaBbLE-Grimoire/registry/entity/rabble.entity.json");
    let Ok(text) = std::fs::read_to_string(p) else { return };
    let reg: serde_json::Value = serde_json::from_str(&text).unwrap();
    assert_eq!(reg["version"], nebula_glyph::vocab::REGISTRY_VERSION, "registry moved: refresh vocab.rs ALIASES");
    let want = reg["aliases"]["score_tui"].as_object().unwrap();
    assert_eq!(want.len(), ALIASES.len());
    for (k, v) in ALIASES {
        assert_eq!(want[k], v, "{k}");
    }
}

#[test]
fn thinking_ponders_then_processes_and_insight_peaks() {
    let mut d = Driver::new();
    d.set_beat(Beat::THINKING, 10.0);
    assert_eq!(d.mood_at(10.5), Mood::Ponder);
    assert_eq!(d.mood_at(11.6), Mood::Process);
    d.tick(11.6, 11.6);
    assert_eq!(d.life.speed, Mood::Process.speed());
    d.insight(12.0);
    assert_eq!(d.mood_at(12.2), Mood::Insight);
    d.tick(13.0, 13.0);
    assert_eq!(d.mood_at(13.0), Mood::Process, "the peak lapses back to the state's own mood");
}

// ---- moves ----

#[test]
fn moves_step_at_8hz_over_half_a_second() {
    let ps: Vec<f64> = (0..=8).map(|i| Move::PortalSwap.progress(i as f64 / 16.0)).collect();
    assert_eq!(ps, [0.0, 0.0, 0.25, 0.25, 0.5, 0.5, 0.75, 0.75, 1.0]);
    assert_eq!(STEP_HZ, 8.0);
    assert_eq!(Move::Boot.progress(0.99), 7.0 / 8.0);
    assert_eq!(Move::Boot.progress(5.0), 1.0);
}

fn ellipse_hits(a: (f64, f64, f64, f64), b: (f64, f64, f64, f64)) -> bool {
    let inside = |e: (f64, f64, f64, f64), x: f64, y: f64| ((x - e.0) / e.2).powi(2) + ((y - e.1) / e.3).powi(2) <= 1.0;
    let n = 120;
    (0..=n).any(|i| {
        let th = std::f64::consts::TAU * i as f64 / n as f64;
        let (x, y) = (a.0 + a.2 * th.cos(), a.1 + a.3 * th.sin());
        inside(b, x, y)
    }) || (0..=n).any(|i| {
        let th = std::f64::consts::TAU * i as f64 / n as f64;
        inside(a, b.0 + b.2 * th.cos(), b.1 + b.3 * th.sin())
    })
}

fn portals(p: &Pose) -> ((f64, f64, f64, f64), (f64, f64, f64, f64)) {
    let (lx, rx) = moves::portal_x(p);
    let (lw, rw) = moves::portal_rx(p);
    ((lx, -POR_OFF + p.p_l.dy, lw, POR_RY * p.p_l.sy), (rx, POR_OFF + p.p_r.dy, rw, POR_RY * p.p_r.sy))
}

#[test]
fn the_swap_ends_mirrored_and_the_portals_never_touch() {
    let base = Expr::Calm.pose();
    let end = moves::swap_at(&base, 1.0);
    assert_eq!(end, moves::mirror(&base));
    assert_eq!(moves::mirror(&end), base, "mirror is its own inverse");
    assert_eq!(moves::swap_at(&base, 0.0), base);
    for i in 0..=400 {
        let p = moves::swap_at(&base, i as f64 / 400.0);
        let (l, r) = portals(&p);
        assert!(!ellipse_hits(l, r), "portals touch at p={}", i as f64 / 400.0);
    }
    // the registry seed (arc 0) would have them cross: this is why the glyph bows sideways
    let mut no_arc = false;
    for i in 0..=400 {
        let mut p = moves::swap_at(&base, i as f64 / 400.0);
        p.p_l.dx = 0.0;
        p.p_r.dx = 0.0;
        let (l, r) = portals(&p);
        no_arc |= ellipse_hits(l, r);
    }
    assert!(no_arc, "without the arc the portals do pass through each other");
    assert!(POR_RX > 0.0);
}

#[test]
fn the_eyes_withdraw_at_mid_swap_and_return() {
    let base = Expr::Calm.pose();
    assert!(moves::swap_at(&base, 0.5).e_l.sy < 1e-9);
    assert!(moves::swap_at(&base, 1.0).e_l.sy > 0.999);
}

#[test]
fn the_turn_trades_the_eyes_through_the_middle_and_ends_where_it_began() {
    let base = Expr::Calm.pose();
    let (a, m, z) = (moves::turn_at(&base, 0.0), moves::turn_at(&base, 0.5), moves::turn_at(&base, 1.0));
    assert_eq!((a.e_l.dx, a.e_l.sx), (0.0, 1.0));
    assert!((z.e_l.dx).abs() < 1e-9 && (z.e_r.dx).abs() < 1e-9 && (z.e_l.sx - 1.0).abs() < 1e-9);
    // at mid-turn both eyes sit on the middle line as slits, colour still by side
    assert!((m.e_l.dx - 92.0).abs() < 1e-9 && (m.e_r.dx + 92.0).abs() < 1e-9);
    assert!((m.e_l.sx - moves::TURN_MIN_W).abs() < 1e-9);
    let q = moves::turn_at(&base, 0.25);
    let r = moves::turn_at(&base, 0.75);
    assert!((q.e_l.dx - r.e_l.dx).abs() < 1e-9 && (q.e_l.sx - r.e_l.sx).abs() < 1e-9, "the move is symmetric about its middle");
}

#[test]
fn boot_brings_the_portals_in_first_then_the_eyes() {
    let base = Expr::Calm.pose();
    let f = |i: usize| moves::boot_at(&base, Move::Boot.progress(i as f64 / STEP_HZ));
    let first = f(0);
    assert!(first.p_l.sx < 0.05 && first.e_l.sy < 0.05);
    let mid = f(4); // 0.5 s: portals in, eyes not yet
    assert!(mid.p_l.sx > 0.99 && mid.p_r.sx > 0.99, "{:?}", mid.p_r);
    assert!(mid.e_l.sy < 0.05, "eyes wait for the portals");
    let late = f(7); // 0.875 s: eyes mid rise
    assert!(late.e_l.sy > 0.5 && late.e_l.sy < 1.0, "{}", late.e_l.sy);
    assert!(f(8) == base);
    // cyan's portal leads magenta's
    assert!(f(2).p_l.sx > f(2).p_r.sx);
}

#[test]
fn the_driver_plays_a_swap_and_stays_swapped() {
    let mut d = Driver::new();
    let base = d.pose();
    d.play(Move::PortalSwap, 1.0);
    d.tick(1.0, 1.0);
    assert_eq!(d.move_frame(), Some((Move::PortalSwap, 0.0)));
    d.tick(1.25, 1.25);
    assert_eq!(d.move_frame(), Some((Move::PortalSwap, 0.5)));
    d.tick(1.6, 1.6);
    assert_eq!(d.move_frame(), None);
    assert!(d.swapped());
    assert_eq!(d.pose(), moves::mirror(&base), "the end frame equals the form it leaves behind: no jump");
    d.play(Move::PortalSwap, 2.0);
    d.tick(2.6, 2.6);
    assert!(!d.swapped());
    assert_eq!(d.pose(), base);
    // expression changes while swapped keep the form
    d.play(Move::PortalSwap, 3.0);
    d.tick(3.6, 3.6);
    d.set_beat(Beat::ERROR, 3.6);
    for i in 0..200 {
        d.tick(3.6 + i as f64 * 0.05, 3.6 + i as f64 * 0.05);
    }
    assert_eq!(d.pose(), moves::mirror(&Expr::Sad.pose()));
}

// ---- rendering ----

fn frame(face: &mut Face, d: &Driver, t: f64) -> String {
    face.pose = Some(d.pose());
    let g = face.frame(t, 0.0, (0.0, 0.0));
    ansi(&g, None)
}

#[test]
fn infinity_is_off_by_default_and_full_only() {
    let mut a = Face::new(60, 16, pal(), Detail::Full, true);
    let off = a.frame(0.0, 0.0, (0.0, 0.0));
    a.infinity = true;
    let on = a.frame(0.0, 0.0, (0.0, 0.0));
    assert_ne!(off, on);
    let mut m = Face::new(24, 6, pal(), Detail::Mini, true);
    let off = m.frame(0.0, 0.0, (0.0, 0.0));
    m.infinity = true;
    assert_eq!(off, m.frame(0.0, 0.0, (0.0, 0.0)), "no room in the mini mark");
}

#[test]
fn every_move_frame_renders() {
    for mv in [Move::PortalSwap, Move::Turn, Move::Boot] {
        let mut d = Driver::new();
        let mut f = Face::new(60, 16, pal(), Detail::Full, true);
        d.play(mv, 0.0);
        let mut seen = std::collections::BTreeSet::new();
        for i in 0..=8 {
            let t = i as f64 / 8.0 * mv.seconds().max(0.5);
            d.tick(t, t);
            seen.insert(frame(&mut f, &d, 0.0));
        }
        assert!(seen.len() >= if mv == Move::Turn { 3 } else { 5 }, "{} gives {} distinct frames", mv.name(), seen.len());
    }
}
