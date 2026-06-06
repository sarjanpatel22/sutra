"""Tests for the stats aggregator (percentiles + log shaping). No real logs needed."""

import json

from friday.core import stats
from friday.core.stats import _load, _metric, percentile


def test_percentile_basics():
    assert percentile([], 50) == 0.0
    assert percentile([42], 95) == 42.0
    assert percentile([0, 10], 50) == 5.0           # midpoint interpolation
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4, 5], 100) == 5.0


def test_metric_ignores_non_numeric_and_missing():
    recs = [{"total_ms": 100}, {"total_ms": 300}, {"total_ms": None}, {"other": 1}]
    m = _metric(recs, "total_ms")
    assert m["n"] == 2
    assert m["p50"] == 200.0


def test_load_tolerates_blanks_and_corruption(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text(json.dumps({"a": 1}) + "\n\n{bad json\n" + json.dumps({"a": 2}) + "\n")
    assert _load(p) == [{"a": 1}, {"a": 2}]


def test_load_missing_file_is_empty(tmp_path):
    assert _load(tmp_path / "nope.jsonl") == []


def test_gather_stats_groups_text_by_tier(tmp_path, monkeypatch):
    text = tmp_path / "turn.jsonl"
    text.write_text(
        json.dumps({"tier": "medium", "total_ms": 100, "in_tokens": 10,
                    "out_tokens": 5, "tool_rounds": 1}) + "\n"
        + json.dumps({"tier": "medium", "total_ms": 300, "in_tokens": 20,
                      "out_tokens": 0, "tool_rounds": 0}) + "\n"
        + json.dumps({"tier": "heavy", "total_ms": 900, "in_tokens": 1,
                      "out_tokens": 1, "tool_rounds": 2}) + "\n"
    )
    monkeypatch.setattr(stats, "TEXT_LATENCY_LOG", text)
    monkeypatch.setattr(stats, "VOICE_LATENCY_LOG", tmp_path / "none.jsonl")

    s = stats.gather_stats()
    assert s["text_turns"] == 3 and s["voice_turns"] == 0
    med = s["text_by_tier"]["medium"]
    assert med["n"] == 2 and med["p50"] == 200.0 and med["tokens"] == 35
    assert med["avg_rounds"] == 0.5
    assert s["text_by_tier"]["heavy"]["tokens"] == 2
