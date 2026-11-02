"""Dump the face's golden fixtures for the Rust port (tui/rabble-face/tests/golden.rs).

    python tests/golden_face.py            # writes tests/fixtures/face/golden.json

Run it with the project venv (it needs textual, which ui/face.py imports). The matrix is fixed here; the
fixture is checked in, and the Rust tests compare cell for cell (glyph exact, colour within a tolerance
that only covers float-order differences). Regenerate whenever ui/face.py or ui/eyes.py changes drawing.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ui import face as F  # noqa: E402
from ui.prng import SplitMix64  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "face" / "golden.json"
PAL_KEYS = ("bg", "text", "cyan", "magenta", "violet", "pink", "muted", "brand_anthropic", "brand_google_right", "brand_google_bottom",
            "brand_google_left", "brand_google_top", "brand_openai")


def hexrow(cells, idx):
    return "".join("%02x%02x%02x" % cell[idx] for cell in cells)


def enc(grid):
    return {"ch": ["".join(c[0] for c in row) for row in grid],
            "fg": [hexrow(row, 1) for row in grid], "bg": [hexrow(row, 2) for row in grid]}


def make_face(spec, pal):
    f = F.Face(spec["cols"], spec["rows"], pal, detail=spec["detail"], safe=spec.get("safe", False))
    f.set_provider(spec.get("provider"))
    f.block = spec.get("block", 1)
    pose = spec.get("pose")
    if pose:
        f.pose = F.blend_pose(F.EXPR[pose["from"]], F.EXPR[pose["to"]], pose["p"])
        f.expr = pose["to"] if pose["p"] > 0.5 else pose["from"]
    else:
        f.expr = spec.get("expr", "calm")
    return f


def frame_specs():
    s = []

    def add(**kw):
        kw.setdefault("t", 0.0)
        kw.setdefault("blink", 0.0)
        kw.setdefault("gaze", [0.0, 0.0])
        s.append(kw)

    for expr in F.EXPR:
        add(kind="frame", detail="mini", cols=24, rows=6, expr=expr, t=1.0)
    add(kind="frame", detail="mini", cols=24, rows=6, t=0.4, blink=0.6, gaze=[0.6, -0.4])
    add(kind="frame", detail="mini", cols=24, rows=6, t=2.0, expr="angry", gaze=[-0.8, 0.3], safe=True)
    add(kind="frame", detail="mini", cols=24, rows=6, t=2.0, expr="sad", block=2)
    add(kind="frame", detail="mini", cols=24, rows=6, t=2.0, pose={"from": "joy", "to": "sad", "p": 0.3})
    add(kind="frame", detail="mini", cols=24, rows=6, t=2.0, pose={"from": "calm", "to": "angry", "p": 0.8})
    for size in ((60, 20), (100, 30)):
        c, r = size
        for t, blink, gaze in ((0.0, 0.0, [0, 0]), (1.3, 0.0, [0.6, -0.4]), (2.2, 0.7, [-0.8, 0.3])):
            add(kind="frame", detail="full", cols=c, rows=r, t=t, blink=blink, gaze=gaze)
        for expr in ("joy", "sad", "angry", "listening", "speaking"):
            add(kind="frame", detail="full", cols=c, rows=r, expr=expr, t=0.8, gaze=[0.2, 0.1])
        for prov in ("anthropic", "google", "openai"):
            add(kind="frame", detail="full", cols=c, rows=r, t=0.5, provider=prov)
        add(kind="frame", detail="full", cols=c, rows=r, t=0.5, safe=True, gaze=[0.3, 0.1])
        add(kind="frame", detail="full", cols=c, rows=r, t=0.5, block=2)
        add(kind="frame", detail="full", cols=c, rows=r, t=0.5, block=3, provider="google")
        add(kind="frame", detail="full", cols=c, rows=r, t=1.1, pose={"from": "calm", "to": "listening", "p": 0.5})
        add(kind="compose", detail="full", cols=c, rows=r, t=1.3, gaze=[0.6, -0.4], provider="anthropic")
    for t in (0.0, 3.7):
        add(kind="frame", detail="full", cols=220, rows=60, t=t, gaze=[0.5, -0.2], provider="openai")
    add(kind="compose", detail="full", cols=220, rows=60, t=2.2, gaze=[-0.8, 0.3])
    add(kind="frame", detail="full", cols=8, rows=4, t=0.3)        # tiny
    add(kind="frame", detail="full", cols=150, rows=22, t=1.0)     # wide and short
    return s


def render(spec, pal):
    f = make_face(spec, pal)
    t, blink, gaze = spec["t"], spec["blink"], tuple(spec["gaze"])
    if spec["kind"] == "compose":
        return f.compose(t, blink, gaze, base=f.background(t))
    return f.frame(t, blink, gaze).cells()


def main():
    pal = F.load_palette()
    out = {"palette": {k: list(pal[k]) for k in PAL_KEYS}}
    out["octant"] = "".join(F._OCTANT[i] for i in range(256))
    out["prng"] = {"seed7": [SplitMix64(7).next_u64() for _ in range(1)], "lattice": [F._lattice(1, i) for i in range(-3, 4)]}
    out["provider_of"] = [[m, loc, F.provider_of(m, loc)] for m, loc in (
        (None, False), ("", False), ("claude-opus-5-5", False), ("claude-opus-5-5", True), ("gemini-3-pro", False),
        ("gemma-2", False), ("gpt-6.1", False), ("o3-mini", False), ("chatgpt-x", False), ("codex-1", False),
        ("some-gpt-4", False), ("llama-4", False), ("Fable-1", False), ("HAIKU", False))]
    out["moods"] = {k: list(v) for k, v in F.MOODS.items()}
    out["curves"] = [[x, F.noise_level(x), F.break_level(x), F.eye_block(F.noise_level(x))]
                     for x in (100, 95, 90, 85, 80, 75, 70, 55, 40, 35, 20, 0, 120, -5)]
    out["blend"] = [{"from": a, "to": b, "p": p, "pose": F.blend_pose(F.EXPR[a], F.EXPR[b], p)}
                    for a, b, p in (("calm", "listening", 0.5), ("joy", "angry", 0.25), ("sad", "speaking", 1.0))]
    rigs = []
    for detail, size in (("mini", (24, 6)), ("full", (100, 30)), ("full", (200, 55)), ("full", (60, 20))):
        for expr, t, blink, gaze, snap in (("calm", 0.0, 0.0, (0, 0), False), ("joy", 1.7, 0.4, (0.5, -0.3), False),
                                           ("angry", 0.9, 0.0, (-1, 0.8), True)):
            f = F.Face(size[0], size[1], pal, detail=detail)
            f.expr = expr
            r = f.rig(t, blink, gaze, snap=snap)
            rigs.append({"detail": detail, "cols": size[0], "rows": size[1], "expr": expr, "t": t, "blink": blink,
                         "gaze": list(gaze), "snap": snap, "rig": {k: list(v) for k, v in r.items()}})
    out["rig"] = rigs
    meshes = []
    for size in ((60, 20), (100, 30), (220, 60)):
        f = F.Face(*size, pal, detail="full")
        nodes, edges = f._mesh
        meshes.append({"cols": size[0], "rows": size[1], "nodes": [list(n) for n in nodes], "edges": [list(e) for e in edges]})
    out["mesh"] = meshes
    life, lf = [], F.Life()
    t, dt = 0.0, 1 / 30
    for i in range(600):
        if i == 300:
            lf.speed = 2.5
        t += dt
        blink, gaze = lf.step(t, dt)
        life.append([blink, gaze[0], gaze[1]])
    out["life"] = life
    frames = []
    for spec in frame_specs():
        e = dict(spec)
        e["cells"] = enc(render(spec, pal))
        frames.append(e)
    out["frames"] = frames
    # degrade: four consecutive frames through one rng + state, as the widget runs them
    deg = []
    base_spec = {"kind": "frame", "detail": "full", "cols": 40, "rows": 14, "t": 0.6, "blink": 0.0, "gaze": [0.2, 0.1]}
    cells = render(base_spec, pal)
    for brk, noise in ((0.0, 0.0), (0.0, 0.5), (0.7, 0.0), (0.9, 0.4), (0.3, 1.0)):
        rng, state, steps = SplitMix64(11), {}, []
        for _ in range(6):
            steps.append(enc(F.degrade(cells, brk, pal, rng, state, noise)))
        deg.append({"brk": brk, "noise": noise, "spec": base_spec, "steps": steps})
    out["degrade"] = deg
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    print(f"{OUT} {OUT.stat().st_size // 1024} KiB, {len(frames)} frames, {len(deg)} degrade runs")


if __name__ == "__main__":
    main()
