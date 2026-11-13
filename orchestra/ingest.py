"""Capability registry from benchmark rows: `python -m orchestra.ingest <benchmarks.json>`.

The curated file (a person, or a reviewed agent run, enters every row with its source; nothing is
scraped) holds benchmarks (id, category, name, version), models (key, match fragments) and results
(model, benchmark, raw score, date, source). This turns the rows into the 0-100 category numbers in
orchestra/capabilities.json, and writes orchestra/capabilities.lock, the published scale a session reads.

  frontier mode (default): per benchmark, the best known score is the reference and maps to `frontier_at`;
      everything else scales against it. A new frontier model rescales everyone, thresholds like
      trusted >= 60 stay meaningful, and the lock records which reference each number was scaled against.
  absolute mode: score / benchmark max * 100, numbers stay put as the frontier saturates.

  elo benchmarks (`"kind": "elo"`, arena ratings): a ratio of ratings says nothing (1400 vs 1500 is 93%), so a model
      is scored by its expected result against the frontier, 2 / (1 + 10^(gap/400)): parity 1.0, 200 below 0.48.
      The reference is the whole table's best rating (`"reference"` on the benchmark), not just the models listed.

A category's score is the weighted mean of its benchmarks. A category with no rows is inferred from the
model's other categories and says so (`inferred`). Models with no rows keep their estimate untouched.

Pure data: no SDK, no UI. Deterministic: the same file gives the same registry.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import capabilities as caps

HERE = Path(__file__).parent
FRONTIER_AT = 95


def _num(x: float) -> int:
    return max(0, min(100, round(x)))


def normalise(data: dict) -> tuple[dict[str, dict], dict]:
    """(model key -> registry fields, scale record) from a curated benchmark file."""
    scale = {"mode": "frontier", "frontier_at": FRONTIER_AT, "reference": "unset", **data.get("scale", {})}
    if scale["mode"] not in ("frontier", "absolute"):
        raise ValueError(f"scale.mode must be frontier or absolute, not {scale['mode']!r}")
    bench = {b["id"]: b for b in data.get("benchmarks", [])}
    models = {m["key"]: m for m in data.get("models", [])}
    rows: dict[str, list[dict]] = {}
    for r in data.get("results", []):
        if r["benchmark"] not in bench:
            raise ValueError(f"result for unknown benchmark {r['benchmark']!r}")
        if r["model"] not in models:
            raise ValueError(f"result for unknown model {r['model']!r}")
        if not r.get("source"):
            raise ValueError(f"result {r['model']}/{r['benchmark']} has no source: every number must be traceable")
        rows.setdefault(r["model"], []).append(r)
    best = {b: float(spec["reference"]) for b, spec in bench.items() if spec.get("reference") is not None}
    for model_rows in rows.values():
        for r in model_rows:
            best[r["benchmark"]] = max(best.get(r["benchmark"], 0.0), float(r["score"]))
    scale["reference_scores"] = {k: v for k, v in sorted(best.items())}

    def norm(r: dict) -> float:
        b = bench[r["benchmark"]]
        if b.get("kind") == "elo":
            gap = best[r["benchmark"]] - float(r["score"])
            return 2 / (1 + 10 ** (gap / 400)) * (scale["frontier_at"] if scale["mode"] == "frontier" else 100)
        if scale["mode"] == "frontier":
            return float(r["score"]) / best[r["benchmark"]] * scale["frontier_at"] if best[r["benchmark"]] else 0.0
        return float(r["score"]) / float(b.get("max", 100)) * 100

    out = {}
    for key, model_rows in rows.items():
        per: dict[str, list[tuple[float, float]]] = {c: [] for c in caps.CATEGORIES}
        for r in model_rows:
            b = bench[r["benchmark"]]
            per[b["category"]].append((norm(r), float(b.get("weight", 1))))
        got = {c: sum(v * w for v, w in vs) / sum(w for _, w in vs) for c, vs in per.items() if vs}
        mean = sum(got.values()) / len(got)
        inferred = [c for c in caps.CATEGORIES if c not in got]
        m = models[key]
        out[key] = {"key": key, "match": m.get("match", [key]), "local": bool(m.get("local", False)),
                    **{k: m[k] for k in ("family", "version") if k in m},
                    **{c: _num(got.get(c, mean)) for c in caps.CATEGORIES},
                    "source": f"benchmarks, scale {scale['mode']} {scale['reference']}",
                    **({"inferred": inferred} if inferred else {}),
                    "rows": [{k: r[k] for k in ("benchmark", "score", "date", "source") if k in r} for r in model_rows]}
    return out, scale


def apply(data: dict, registry: dict) -> tuple[dict, dict]:
    """The registry with the benchmarked models replaced (others untouched), and the lock record."""
    scores, scale = normalise(data)
    models = [dict(m) for m in registry.get("models", [])]
    for key, entry in scores.items():
        for i, old in enumerate(models):
            if old.get("key") == key:
                models[i] = {**old, **entry}
                break
        else:
            models.insert(0, entry)
    new = {**registry, "models": models, "scale": {k: v for k, v in scale.items()}}
    lock = {"scale": scale, "models": {k: {c: v[c] for c in caps.CATEGORIES} for k, v in scores.items()},
            "input_sha256": hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()}
    return new, lock


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("benchmarks", type=Path, help="the curated benchmark rows (JSON)")
    ap.add_argument("--registry", type=Path, default=HERE / "capabilities.json")
    ap.add_argument("--lock", type=Path, default=HERE / "capabilities.lock")
    ap.add_argument("--check", action="store_true", help="show what would change, write nothing")
    args = ap.parse_args(argv)
    data = json.loads(args.benchmarks.read_text(encoding="utf-8"))
    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    new, lock = apply(data, registry)
    old = {m["key"]: m for m in registry.get("models", [])}
    for key, scores in lock["models"].items():
        before = old.get(key)
        was = "new" if not before else " ".join(f"{c} {before.get(c)}" for c in caps.CATEGORIES)
        print(f"{key}: {was}  ->  " + " ".join(f"{c} {scores[c]}" for c in caps.CATEGORIES))
    if not lock["models"]:
        print("no results in the file: registry unchanged")
    if args.check:
        return 0
    args.registry.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    args.lock.write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
