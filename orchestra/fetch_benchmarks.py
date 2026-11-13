"""Curated benchmark file from an open source: the LMArena leaderboard dataset on Hugging Face.

    uv run --with pyarrow python -m orchestra.fetch_benchmarks [-o benchmarks.json]     # then: python -m orchestra.ingest benchmarks.json --check

Source: https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset (public, refreshed weekly, every row carries
its publish date). One parquet download per config (needs pyarrow: `uv run --with pyarrow python -m orchestra.fetch_benchmarks`).

  general  text / overall                    elo
  coding   text / coding, webdev / overall   elo
  tools    (none yet: the LMArena agent board disagrees with vendor Terminal-Bench / OSWorld tables; inferred)
  recall   text_factuality / overall         elo

Every version of each family (fable, opus, sonnet, haiku, gpt) in the table becomes its own registry entry
(opus-4.6 is not opus-5.5); of a version's variants (High, Max, xHigh...) the best rated is used, since a variant is
a setting and capability is what the model can do. A version the table lacks falls back to the family estimate.
The scale reference is the best rating in the whole table (every lab), so the numbers are relative to the frontier
and shift when a new one lands. `ingest` does the scaling; this only collects rows and their sources.

`build` is pure (rows in, curated file out) so it is tested offline; `fetch` is the only network code.
"""

import argparse
import json
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

CURATED = Path(__file__).resolve().parents[2] / "RaBbLE-Grimoire" / "RaBbLE-sCoRE" / "RaBbLE-sCoRE-Capability-Benchmarks.json"
DATASET = "lmarena-ai/leaderboard-dataset"
PAGE_URL = "https://huggingface.co/datasets/" + DATASET

# id -> (config, category, registry category, kind, score field)
BENCHMARKS = {
    "lmarena-text": ("text", "overall", "general", "elo", "rating"),
    "lmarena-text-coding": ("text", "coding", "coding", "elo", "rating"),
    "lmarena-webdev": ("webdev", "overall", "coding", "elo", "rating"),
    "lmarena-text-factuality": ("text_factuality", "overall", "recall", "elo", "rating"),
}
# registry family -> name patterns whose first group is the version (new naming, then "Claude 3.5 Sonnet")
FAMILIES = {
    "fable": [r"\bFable (\d+(?:\.\d+)?)"],
    "opus": [r"\bOpus (\d+(?:\.\d+)?)", r"\b(\d+(?:\.\d+)?) Opus"],
    "sonnet": [r"\bSonnet (\d+(?:\.\d+)?)", r"\b(\d+(?:\.\d+)?) Sonnet"],
    "haiku": [r"\bHaiku (\d+(?:\.\d+)?)", r"\b(\d+(?:\.\d+)?) Haiku"],
    "gpt": [r"^GPT-(\d+(?:\.\d+)?)"],
}


def family(name: str) -> tuple[str, str] | None:
    """(family, version) of a leaderboard model name: "Claude Opus 5.5 (High)" -> (opus, "5.5")."""
    for key, pats in FAMILIES.items():
        for pat in pats:
            if m := re.search(pat, name):
                return key, m.group(1)
    return None


def build(tables: dict[str, list[dict]]) -> dict:
    """tables: benchmark id -> that leaderboard's rows. Returns the curated file `ingest` reads."""
    benchmarks, results, dates = [], [], []
    keys: set[tuple[str, str]] = set()
    for bid, (config, category, cat, kind, field) in BENCHMARKS.items():
        rows = [r for r in tables.get(bid, []) if r.get(field) is not None]
        if not rows:
            continue
        reference = max(float(r[field]) for r in rows)
        benchmarks.append({"id": bid, "category": cat, "name": f"LMArena {config} / {category}", "kind": kind,
                           "version": max(str(r.get("leaderboard_publish_date", "")) for r in rows),
                           "reference": reference})
        best: dict[tuple[str, str], dict] = {}
        for r in rows:
            fam = family(r["model_name"])
            if fam and float(r[field]) > float(best.get(fam, {}).get(field, -1)):
                best[fam] = r
        for (fam, ver), r in best.items():
            keys.add((fam, ver))
            date = str(r.get("leaderboard_publish_date", ""))
            dates.append(date)
            results.append({"model": f"{fam}-{ver}", "benchmark": bid, "score": float(r[field]), "date": date,
                            "source": f"{PAGE_URL} (config {config}, category {category}, row \"{r['model_name']}\")"})
    return {
        "scale": {"mode": "frontier", "reference": f"LMArena leaderboard {max(dates) if dates else 'n/a'}"},
        "benchmarks": benchmarks,
        "models": [{"key": f"{f}-{v}", "family": f, "version": v, "match": []} for f, v in sorted(keys)],
        "results": results,
    }


def _download(url: str, dest: Path, tries: int = 4) -> None:
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                dest.write_bytes(resp.read())
            return
        except (urllib.error.URLError, TimeoutError):
            if attempt == tries - 1:
                raise
            time.sleep(2 ** attempt)


def fetch_rows(config: str, category: str, workdir: Path) -> list[dict]:
    """One parquet per config (a few hundred KB), filtered to the category. Needs pyarrow: run through
    `uv run --with pyarrow` (spells/ingest-capabilities.sh does), so the app venv stays dependency-free."""
    import pyarrow.parquet as pq
    path = workdir / f"{config}.parquet"
    if not path.exists():
        _download(f"{PAGE_URL}/resolve/main/{config}/latest-00000-of-00001.parquet", path)
    return [r for r in pq.read_table(path).to_pylist() if r.get("category") == category]


def fetch(log=print) -> dict:
    tables = {}
    with tempfile.TemporaryDirectory(prefix="rabble-bench-") as tmp:
        for bid, (config, category, *_rest) in BENCHMARKS.items():
            tables[bid] = fetch_rows(config, category, Path(tmp))
            log(f"{bid}: {len(tables[bid])} rows")
    return build(tables)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-o", "--out", type=Path, default=CURATED)
    args = ap.parse_args(argv)
    data = fetch(log=lambda m: print(m, file=sys.stderr))
    if not data["results"]:
        print("no usable rows fetched; nothing written", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({len(data['results'])} rows); preview: python -m orchestra.ingest {args.out} --check", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
