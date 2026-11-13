from orchestra import fetch_benchmarks as fb
from orchestra import ingest


def row(name, rating, date="2026-10-22", field="rating"):
    return {"model_name": name, "organization": "x", field: rating, "leaderboard_publish_date": date}


TABLES = {
    "lmarena-text": [row("Gemini 4 Argon (High)", 1500), row("Claude Opus 5.5 (High)", 1490), row("Claude Opus 5.5 (Max)", 1480),
                     row("Claude Opus 4.8 (High)", 1495), row("Claude Haiku 5.5 (High)", 1300), row("GPT-6 Astra (Max)", 1450),
                     row("Claude 3.5 Sonnet (20241022)", 1280)],
}


def test_family_and_versions():
    assert fb.family("Claude Opus 5.5 (High)") == ("opus", "5.5")
    assert fb.family("Claude Opus 5 (Max)") == ("opus", "5")
    assert fb.family("Claude 3.5 Sonnet (20241022)") == ("sonnet", "3.5")
    assert fb.family("GPT-6.1 Sol (Max)") == ("gpt", "6.1")
    assert fb.family("Gemini 4 Argon") is None and fb.family("Some GPT-like thing") is None


def test_every_version_is_its_own_entry_with_best_variant():
    data = fb.build(TABLES)
    got = {r["model"]: r["score"] for r in data["results"]}
    assert got["opus-5.5"] == 1490.0 and got["opus-4.8"] == 1495.0       # separate versions, best variant of each
    assert {m["key"] for m in data["models"]} == {"opus-5.5", "opus-4.8", "haiku-5.5", "gpt-6", "sonnet-3.5"}
    assert all(m["match"] == [] and m["family"] and m["version"] for m in data["models"])
    assert all(r["source"] and r["date"] for r in data["results"])
    assert next(b for b in data["benchmarks"] if b["id"] == "lmarena-text")["reference"] == 1500.0   # the whole table


def test_elo_scales_against_frontier():
    scores, scale = ingest.normalise(fb.build(TABLES))
    assert scores["opus-4.8"]["general"] > scores["opus-5.5"]["general"] > scores["gpt-6"]["general"] > scores["haiku-5.5"]["general"]
    assert scores["opus-5.5"]["general"] == round(2 / (1 + 10 ** (10 / 400)) * 95)   # 10 below the frontier ~ 94
    assert "tools" in scores["opus-5.5"]["inferred"]               # no tools benchmark yet
    assert scale["reference_scores"]["lmarena-text"] == 1500.0


def test_empty_tables_make_no_results():
    assert fb.build({})["results"] == []
