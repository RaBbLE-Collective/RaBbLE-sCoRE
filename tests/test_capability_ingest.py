"""orchestra/ingest.py: benchmark rows -> the capability registry. The numbers below are a synthetic
fixture for the maths, not measurements of any real model."""

import copy
import json

import pytest

from orchestra import capabilities as caps
from orchestra import ingest

DATA = {
    "scale": {"mode": "frontier", "frontier_at": 95, "reference": "fixture-1"},
    "benchmarks": [
        {"id": "g1", "category": "general", "name": "G", "version": "1", "max": 100},
        {"id": "c1", "category": "coding", "name": "C", "version": "1", "max": 100},
        {"id": "c2", "category": "coding", "name": "C2", "version": "1", "max": 100, "weight": 3},
    ],
    "models": [{"key": "big", "match": ["big"]}, {"key": "small", "match": ["small"], "local": True}],
    "results": [
        {"model": "big", "benchmark": "g1", "score": 80, "date": "2026-10-01", "source": "fixture"},
        {"model": "big", "benchmark": "c1", "score": 60, "date": "2026-10-01", "source": "fixture"},
        {"model": "small", "benchmark": "g1", "score": 40, "date": "2026-10-01", "source": "fixture"},
        {"model": "small", "benchmark": "c1", "score": 30, "date": "2026-10-01", "source": "fixture"},
        {"model": "small", "benchmark": "c2", "score": 10, "date": "2026-10-01", "source": "fixture"},
    ],
}


def test_frontier_scale_maps_the_best_known_to_the_cap():
    out, scale = ingest.normalise(DATA)
    assert out["big"]["general"] == 95 and out["big"]["coding"] == 95      # best on every benchmark it ran
    assert out["small"]["general"] == 48                                    # 40/80 * 95
    assert scale["reference_scores"] == {"c1": 60.0, "c2": 10.0, "g1": 80.0}
    assert "tools" in out["big"].get("inferred", []) and "recall" in out["big"]["inferred"]
    assert out["big"]["tools"] == 95 and out["small"]["local"] is True


def test_weighted_mean_within_a_category():
    out, _ = ingest.normalise(DATA)
    # small coding: c1 -> 30/60*95 = 47.5 (w1), c2 -> 10/10*95 = 95 (w3): (47.5 + 285) / 4 = 83.1
    assert out["small"]["coding"] == 83


def test_a_new_frontier_rescales_everyone_and_the_lock_records_it():
    data = copy.deepcopy(DATA)
    data["models"].append({"key": "next", "match": ["next"]})
    data["results"].append({"model": "next", "benchmark": "g1", "score": 160, "date": "2026-11-01", "source": "fixture"})
    before, _ = ingest.normalise(DATA)
    after, scale = ingest.normalise(data)
    assert after["next"]["general"] == 95 and after["big"]["general"] < before["big"]["general"] == 95
    assert scale["reference_scores"]["g1"] == 160.0


def test_absolute_scale_leaves_numbers_where_they_are():
    data = copy.deepcopy(DATA); data["scale"]["mode"] = "absolute"
    out, _ = ingest.normalise(data)
    assert out["big"]["general"] == 80 and out["small"]["general"] == 40


def test_every_number_needs_a_source_and_known_ids():
    bad = copy.deepcopy(DATA); del bad["results"][0]["source"]
    with pytest.raises(ValueError, match="no source"):
        ingest.normalise(bad)
    bad = copy.deepcopy(DATA); bad["results"][0]["benchmark"] = "zzz"
    with pytest.raises(ValueError, match="unknown benchmark"):
        ingest.normalise(bad)


def test_apply_replaces_only_benchmarked_models_and_keeps_the_rest(tmp_path):
    reg = json.loads(caps._SHIPPED.read_text())
    new, lock = ingest.apply(DATA, reg)
    keys = [m["key"] for m in new["models"]]
    assert "opus" in keys and "big" in keys and new["scale"]["reference"] == "fixture-1"
    assert next(m for m in new["models"] if m["key"] == "opus") == next(m for m in reg["models"] if m["key"] == "opus")
    assert lock["models"]["big"]["general"] == 95 and len(lock["input_sha256"]) == 64
    # an existing model is replaced in place and a registry built from the result still loads
    data = copy.deepcopy(DATA); data["models"][0] = {"key": "opus", "match": ["opus"]}
    for r in data["results"]:
        if r["model"] == "big":
            r["model"] = "opus"
    new, _ = ingest.apply(data, reg)
    p = tmp_path / "caps.json"; p.write_text(json.dumps(new))
    assert caps.Registry(shipped=p, user=tmp_path / "none.json").lookup("claude-opus-5-5").general == 95


def test_cli_writes_registry_and_lock_and_check_writes_nothing(tmp_path, capsys):
    src, reg, lock = tmp_path / "b.json", tmp_path / "caps.json", tmp_path / "caps.lock"
    src.write_text(json.dumps(DATA)); reg.write_text(caps._SHIPPED.read_text())
    args = [str(src), "--registry", str(reg), "--lock", str(lock)]
    assert ingest.main(args + ["--check"]) == 0 and not lock.exists()
    assert ingest.main(args) == 0 and json.loads(lock.read_text())["scale"]["reference"] == "fixture-1"
    assert "big" in capsys.readouterr().out
    empty = {**DATA, "results": []}
    src.write_text(json.dumps(empty)); before = reg.read_text()
    assert ingest.main(args) == 0 and [m["key"] for m in json.loads(reg.read_text())["models"]] == \
        [m["key"] for m in json.loads(before)["models"]]
