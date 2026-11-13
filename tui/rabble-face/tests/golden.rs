//! Cell-for-cell parity with `ui/face.py` / `ui/eyes.py`, against `tests/fixtures/face/golden.json`
//! (written by `tests/golden_face.py`). Glyphs must match exactly; colours within COLOUR_TOL per channel
//! (float-order differences only), and almost every cell must be exact.

use rabble_face::face::{Expr, Face, FacePalette, Provider, EYE_RX};
use rabble_face::{blend_pose, break_level, degrade, eye_block, noise_level, provider_of, Cell, DegradeState, Detail, Driver, Grid, Life, Mood, SplitMix64};
use rabble_theme::{Palette, Rgb};
use serde_json::Value;
use std::sync::OnceLock;

const COLOUR_TOL: i32 = 1;

fn golden() -> &'static Value {
    static G: OnceLock<Value> = OnceLock::new();
    G.get_or_init(|| {
        let p = concat!(env!("CARGO_MANIFEST_DIR"), "/../../tests/fixtures/face/golden.json");
        serde_json::from_str(&std::fs::read_to_string(p).expect("run tests/golden_face.py")).unwrap()
    })
}

fn palette() -> FacePalette {
    let mut text = String::new();
    for (k, v) in golden()["palette"].as_object().unwrap() {
        let c: Vec<u64> = v.as_array().unwrap().iter().map(|x| x.as_u64().unwrap()).collect();
        text.push_str(&format!("  {k}: \"{}\"\n", Rgb(c[0] as u8, c[1] as u8, c[2] as u8)));
    }
    FacePalette::from_palette(&Palette::parse(&text))
}

fn f64s(v: &Value) -> Vec<f64> {
    v.as_array().unwrap().iter().map(|x| x.as_f64().unwrap()).collect()
}

fn hex(s: &str, i: usize) -> Rgb {
    let p = |k: usize| u8::from_str_radix(&s[i * 6 + k..i * 6 + k + 2], 16).unwrap();
    Rgb(p(0), p(2), p(4))
}

#[derive(Default, Debug)]
struct Diff {
    cells: usize,
    glyph: usize,
    colour_off: usize, // any channel differs at all
    colour_bad: usize, // beyond COLOUR_TOL
    first: Option<String>,
}

fn compare(got: &Grid, want: &Value, label: &str) -> Diff {
    let ch: Vec<&str> = want["ch"].as_array().unwrap().iter().map(|x| x.as_str().unwrap()).collect();
    let fg: Vec<&str> = want["fg"].as_array().unwrap().iter().map(|x| x.as_str().unwrap()).collect();
    let bg: Vec<&str> = want["bg"].as_array().unwrap().iter().map(|x| x.as_str().unwrap()).collect();
    assert_eq!(got.len(), ch.len(), "{label}: rows");
    let mut d = Diff::default();
    for (r, row) in got.iter().enumerate() {
        let chars: Vec<char> = ch[r].chars().collect();
        assert_eq!(row.len(), chars.len(), "{label}: cols");
        for (c, cell) in row.iter().enumerate() {
            d.cells += 1;
            let (wf, wb) = (hex(fg[r], c), hex(bg[r], c));
            let dv = |a: Rgb, b: Rgb| (a.0 as i32 - b.0 as i32).abs().max((a.1 as i32 - b.1 as i32).abs()).max((a.2 as i32 - b.2 as i32).abs());
            let worst = dv(cell.fg, wf).max(dv(cell.bg, wb));
            let g = cell.ch != chars[c];
            d.glyph += g as usize;
            d.colour_off += (worst > 0) as usize;
            d.colour_bad += (worst > COLOUR_TOL) as usize;
            if (g || worst > COLOUR_TOL) && d.first.is_none() {
                d.first = Some(format!("{label} r{r} c{c}: got {:?} {} {} want {:?} {} {}", cell.ch, cell.fg, cell.bg, chars[c], wf, wb));
            }
        }
    }
    d
}

fn face_for(spec: &Value, pal: FacePalette) -> Face {
    let detail = if spec["detail"] == "mini" { Detail::Mini } else { Detail::Full };
    let mut f = Face::with_aspect(spec["cols"].as_u64().unwrap() as usize, spec["rows"].as_u64().unwrap() as usize, pal, detail, spec["safe"].as_bool().unwrap_or(false), spec["aspect"].as_f64().unwrap_or(2.0));
    f.set_provider(spec["provider"].as_str().and_then(Provider::from_name));
    f.block = spec["block"].as_u64().unwrap_or(1) as u32;
    if let Some(p) = spec.get("pose").filter(|p| !p.is_null()) {
        let (a, b) = (Expr::from_name(p["from"].as_str().unwrap()).unwrap(), Expr::from_name(p["to"].as_str().unwrap()).unwrap());
        let pp = p["p"].as_f64().unwrap();
        f.pose = Some(blend_pose(&a.pose(), &b.pose(), pp));
        f.expr = if pp > 0.5 { b } else { a };
    } else {
        f.expr = spec["expr"].as_str().and_then(Expr::from_name).unwrap_or(Expr::Calm);
    }
    f
}

fn render(spec: &Value, pal: FacePalette) -> Grid {
    let f = face_for(spec, pal);
    let (t, blink) = (spec["t"].as_f64().unwrap(), spec["blink"].as_f64().unwrap());
    let g = f64s(&spec["gaze"]);
    if spec["kind"] == "compose" {
        let base = f.background(t);
        f.compose(t, blink, (g[0], g[1]), Some(&base), None).0
    } else {
        f.frame(t, blink, (g[0], g[1]))
    }
}

#[test]
fn octant_table_matches_unicode() {
    let want: Vec<char> = golden()["octant"].as_str().unwrap().chars().collect();
    assert_eq!(want.len(), 256);
    for (i, w) in want.iter().enumerate() {
        assert_eq!(rabble_face::canvas::octant_table()[i], *w, "pattern {i:#010b}");
    }
}

#[test]
fn prng_lattice_and_seed() {
    let g = &golden()["prng"];
    assert_eq!(SplitMix64::new(7).next_u64(), g["seed7"][0].as_u64().unwrap());
    // face.py `_lattice(1, i)` = SplitMix64(1 * 7919 + i).random()
    for (k, want) in f64s(&g["lattice"]).into_iter().enumerate() {
        // serde_json parses floats without the round-trip feature: allow the last ulp
        assert!((SplitMix64::new(7919 + k as i64 - 3).random() - want).abs() < 1e-15);
    }
}

#[test]
fn provider_of_matches() {
    for row in golden()["provider_of"].as_array().unwrap() {
        let m = row[0].as_str();
        let want = row[2].as_str();
        assert_eq!(provider_of(m, row[1].as_bool().unwrap()).map(|p| p.name()), want, "{row}");
    }
}

#[test]
fn moods_curves_and_blend_match() {
    for (k, v) in golden()["moods"].as_object().unwrap() {
        let (e, s) = Mood::from_name(k).info();
        assert_eq!((e.name(), s), (v[0].as_str().unwrap(), v[1].as_f64().unwrap()), "{k}");
    }
    for row in golden()["curves"].as_array().unwrap() {
        let x = row[0].as_f64().unwrap();
        assert!((noise_level(x) - row[1].as_f64().unwrap()).abs() < 1e-12, "noise {x}");
        assert!((break_level(x) - row[2].as_f64().unwrap()).abs() < 1e-12, "break {x}");
        assert_eq!(eye_block(noise_level(x)) as u64, row[3].as_u64().unwrap(), "block {x}");
    }
    for b in golden()["blend"].as_array().unwrap() {
        let (a, c) = (Expr::from_name(b["from"].as_str().unwrap()).unwrap(), Expr::from_name(b["to"].as_str().unwrap()).unwrap());
        let got = blend_pose(&a.pose(), &c.pose(), b["p"].as_f64().unwrap());
        for (name, xf) in [("eL", got.e_l), ("eR", got.e_r), ("pL", got.p_l), ("pR", got.p_r)] {
            let w = &b["pose"][name];
            for (k, v) in [("dx", xf.dx), ("dy", xf.dy), ("rot", xf.rot), ("sx", xf.sx), ("sy", xf.sy)] {
                assert!((v - w[k].as_f64().unwrap()).abs() < 1e-9, "{name}.{k}");
            }
        }
    }
}

#[test]
fn rig_matches_python() {
    for e in golden()["rig"].as_array().unwrap() {
        let detail = if e["detail"] == "mini" { Detail::Mini } else { Detail::Full };
        let mut f = Face::new(e["cols"].as_u64().unwrap() as usize, e["rows"].as_u64().unwrap() as usize, palette(), detail, false);
        f.expr = Expr::from_name(e["expr"].as_str().unwrap()).unwrap();
        let g = f64s(&e["gaze"]);
        let r = f.rig(e["t"].as_f64().unwrap(), e["blink"].as_f64().unwrap(), (g[0], g[1]), e["snap"].as_bool().unwrap());
        for (name, v) in [("eL", r.e_l), ("eR", r.e_r), ("pL", r.p_l), ("pR", r.p_r)] {
            let w = f64s(&e["rig"][name]);
            for (i, got) in [v.0, v.1, v.2, v.3, v.4].into_iter().enumerate() {
                assert!((got - w[i]).abs() < 1e-9, "{e} {name}[{i}]: {got} vs {}", w[i]);
            }
        }
    }
}

/// `test_rig_baseline`: idle proportions from RaBbLE-NeBuLA/specs/rig/entity-rig.json, in eye widths.
#[test]
fn rig_baseline() {
    for (detail, cols, rows) in [(Detail::Mini, 24, 6), (Detail::Full, 100, 30), (Detail::Full, 200, 55)] {
        let r = Face::new(cols, rows, palette(), detail, false).rig(0.0, 0.0, (0.0, 0.0), false);
        let w = 2.0 * r.e_l.2;
        let near = |a: f64, b: f64, tol: f64| assert!((a - b).abs() <= tol, "{a} vs {b} (size {cols}x{rows})");
        near((r.e_r.0 - r.e_l.0) / w, 2.1, 0.05);
        near(2.0 * r.p_l.2 / w, 2.7, 0.05);
        near(r.p_l.3 / r.p_l.2, 0.3, 0.03);
        near((r.p_l.1 - r.e_l.1).abs() / r.e_l.3, 0.93, 0.03);
        assert!(r.p_l.1 < r.e_l.1 && r.e_l.1 < r.p_r.1, "left portal above its eye, right below");
        assert!(r.e_l.1 == r.e_r.1 && r.e_l.2 == r.e_r.2 && r.e_l.3 == r.e_r.3, "level, equal");
    }
    assert!(EYE_RX > 0.0);
}

#[test]
fn mesh_matches_python() {
    for m in golden()["mesh"].as_array().unwrap() {
        let f = Face::new(m["cols"].as_u64().unwrap() as usize, m["rows"].as_u64().unwrap() as usize, palette(), Detail::Full, false);
        let want = m["nodes"].as_array().unwrap();
        assert_eq!(f.mesh_nodes().len(), want.len());
        for (n, w) in f.mesh_nodes().iter().zip(want) {
            let w = f64s(w);
            assert!((n.0 - w[0]).abs() < 1e-9 && (n.1 - w[1]).abs() < 1e-9 && n.2 as f64 == w[2], "{n:?} vs {w:?}");
        }
    }
}

#[test]
fn life_matches_python() {
    let mut l = Life::new(3);
    let (mut t, dt) = (0.0, 1.0 / 30.0);
    for (i, want) in golden()["life"].as_array().unwrap().iter().enumerate() {
        if i == 300 {
            l.speed = 2.5;
        }
        t += dt;
        let (b, g) = l.step(t, dt);
        let w = f64s(want);
        assert!((b - w[0]).abs() < 1e-9 && (g.0 - w[1]).abs() < 1e-9 && (g.1 - w[2]).abs() < 1e-9, "step {i}: {b} {g:?} vs {w:?}");
    }
}

#[test]
fn external_gaze_life_matches_python() {
    let mut l = Life::new(3);
    let (mut t, dt) = (0.0, 1.0 / 30.0);
    for (i, want) in golden()["life_ext"].as_array().unwrap().iter().enumerate() {
        match i {
            60 => l.set_gaze(Some((0.7, -0.3)), t + 1.0),
            150 => l.set_gaze(Some((9.0, -9.0)), f64::INFINITY),
            220 => l.set_gaze(None, 0.0),
            _ => {}
        }
        t += dt;
        let (b, g) = l.step(t, dt);
        let w = f64s(want);
        assert!((b - w[0]).abs() < 1e-9 && (g.0 - w[1]).abs() < 1e-9 && (g.1 - w[2]).abs() < 1e-9, "step {i}: {b} {g:?} vs {w:?}");
    }
}

#[test]
fn frames_match_python_cell_for_cell() {
    let (pal, mut worst_off) = (palette(), 0.0f64);
    let mut bad = Vec::new();
    for spec in golden()["frames"].as_array().unwrap() {
        let label = format!("{} {}x{} t={} {:?}", spec["kind"], spec["cols"], spec["rows"], spec["t"], spec.get("expr").or(spec.get("provider")));
        let d = compare(&render(spec, pal), &spec["cells"], &label);
        worst_off = worst_off.max(d.colour_off as f64 / d.cells as f64);
        if d.glyph > 0 || d.colour_bad > 0 {
            bad.push(format!("{label}: {} glyph, {} colour of {} cells; {:?}", d.glyph, d.colour_bad, d.cells, d.first));
        }
    }
    eprintln!("worst share of cells off by at least one channel unit: {worst_off:.5}");
    assert!(bad.is_empty(), "frames differ from the Python goldens:\n{}", bad.join("\n"));
    assert!(worst_off < 0.02, "too many cells off by even one: {worst_off}");
}

#[test]
fn degrade_matches_python() {
    let pal = palette();
    for run in golden()["degrade"].as_array().unwrap() {
        let base = render(&run["spec"], pal);
        let (mut rng, mut state) = (SplitMix64::new(11), DegradeState::default());
        for (i, want) in run["steps"].as_array().unwrap().iter().enumerate() {
            let got = degrade(&base, run["brk"].as_f64().unwrap(), &pal, &mut rng, &mut state, run["noise"].as_f64().unwrap());
            let d = compare(&got, want, &format!("brk {} noise {} step {i}", run["brk"], run["noise"]));
            assert!(d.glyph == 0 && d.colour_bad == 0, "{d:?}");
        }
    }
}

#[test]
fn driver_eases_and_denied_reverts() {
    let mut d = Driver::new();
    d.set_mood(Mood::Error, 0.0);
    assert_eq!(d.target_expr(), Expr::Sad);
    let mut now = 0.0;
    for _ in 0..30 {
        now += 1.0 / 30.0;
        d.tick(now, now);
    }
    assert!(d.blend() > 0.5 && d.expr() == Expr::Sad);
    d.set_mood(Mood::Denied, now);
    assert_eq!(d.target_expr(), Expr::Angry);
    for _ in 0..90 {
        now += 1.0 / 30.0;
        d.tick(now, now);
    }
    assert_eq!(d.target_expr(), Expr::Calm, "denied reverts to idle after DENIED_SECONDS");
    let _ = Cell { ch: ' ', fg: Rgb(0, 0, 0), bg: Rgb(0, 0, 0) };
}
