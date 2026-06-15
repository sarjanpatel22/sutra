"""Tests for friday.core.turnlog — structured per-turn event log.

All tests use tmp_path fixtures and never touch the real turns.jsonl.
No network, no LLM, no monkeypatching of heavy modules.
"""

from __future__ import annotations

import json
from pathlib import Path

from friday.core.turnlog import (
    _bucket_stats,
    _pct,
    aggregate_turns,
    load_turns,
    log_turn,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_turn(path: Path, **kwargs) -> None:
    """Write a single turn record to an arbitrary jsonl path."""
    defaults = {
        "ts": "2026-06-15T12:00:00",
        "surface": "text",
        "tier": "medium",
        "effort": "",
        "tool_calls": [],
        "tool_count": 0,
        "tokens_in": 10,
        "tokens_out": 5,
        "cost_usd": 0.0,
        "latency_ms": 300,
        "ok": True,
        "error": "",
        "model": "groq/llama-3.3-70b-versatile",
    }
    defaults.update(kwargs)
    with open(path, "a") as f:
        f.write(json.dumps(defaults) + "\n")


# ---------------------------------------------------------------------------
# _pct (percentile helper)
# ---------------------------------------------------------------------------

def test_pct_empty_returns_zero():
    assert _pct([], 50) == 0.0


def test_pct_single_value():
    assert _pct([42.0], 95) == 42.0


def test_pct_midpoint():
    assert _pct([0.0, 10.0], 50) == 5.0


def test_pct_interpolated():
    assert _pct([1.0, 2.0, 3.0, 4.0], 50) == 2.5


def test_pct_p100():
    assert _pct([1.0, 2.0, 5.0], 100) == 5.0


# ---------------------------------------------------------------------------
# log_turn — write + read-back round-trip
# ---------------------------------------------------------------------------

def test_log_turn_round_trip(tmp_path, monkeypatch):
    target = tmp_path / "turns.jsonl"
    monkeypatch.setattr("friday.core.turnlog.TURNS_LOG", target)

    log_turn(
        surface="text",
        tier="medium",
        effort="act",
        tool_calls=["web_search", "run_python"],
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.0,
        latency_ms=450.7,
        ok=True,
        model="groq/llama-3.3-70b-versatile",
    )

    turns = load_turns(target)
    assert len(turns) == 1
    t = turns[0]
    assert t["surface"] == "text"
    assert t["tier"] == "medium"
    assert t["effort"] == "act"
    assert t["tool_calls"] == ["web_search", "run_python"]
    assert t["tool_count"] == 2
    assert t["tokens_in"] == 100
    assert t["tokens_out"] == 50
    assert t["latency_ms"] == 451  # rounded
    assert t["ok"] is True
    assert t["error"] == ""
    assert "ts" in t


def test_log_turn_error_turn(tmp_path, monkeypatch):
    target = tmp_path / "turns.jsonl"
    monkeypatch.setattr("friday.core.turnlog.TURNS_LOG", target)

    log_turn(
        surface="telegram",
        tier="heavy",
        ok=False,
        error="All models in tier heavy failed.",
        latency_ms=1200,
    )

    turns = load_turns(target)
    assert len(turns) == 1
    t = turns[0]
    assert t["ok"] is False
    assert "All models" in t["error"]
    assert t["surface"] == "telegram"


def test_log_turn_multiple_surfaces(tmp_path, monkeypatch):
    target = tmp_path / "turns.jsonl"
    monkeypatch.setattr("friday.core.turnlog.TURNS_LOG", target)

    log_turn(surface="text", tier="medium", latency_ms=300)
    log_turn(surface="voice", tier="local", latency_ms=900)
    log_turn(surface="telegram", tier="medium", latency_ms=400)

    turns = load_turns(target)
    assert len(turns) == 3
    surfaces = {t["surface"] for t in turns}
    assert surfaces == {"text", "voice", "telegram"}


def test_log_turn_swallows_write_error(tmp_path, monkeypatch):
    """A bad path must not raise — logging is always best-effort."""
    monkeypatch.setattr("friday.core.turnlog.TURNS_LOG", Path("/no/such/dir/turns.jsonl"))
    # Should not raise
    log_turn(surface="text", tier="medium")


# ---------------------------------------------------------------------------
# load_turns — tolerant parsing
# ---------------------------------------------------------------------------

def test_load_turns_missing_file(tmp_path):
    assert load_turns(tmp_path / "nonexistent.jsonl") == []


def test_load_turns_tolerates_blank_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        json.dumps({"surface": "text", "ok": True}) + "\n"
        "\n"
        "   \n"
        + json.dumps({"surface": "voice", "ok": True}) + "\n"
    )
    turns = load_turns(p)
    assert len(turns) == 2


def test_load_turns_tolerates_corrupt_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        json.dumps({"surface": "text"}) + "\n"
        "{bad json\n"
        + json.dumps({"surface": "voice"}) + "\n"
    )
    turns = load_turns(p)
    assert len(turns) == 2
    assert turns[0]["surface"] == "text"
    assert turns[1]["surface"] == "voice"


def test_load_turns_empty_file(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("")
    assert load_turns(p) == []


# ---------------------------------------------------------------------------
# _bucket_stats
# ---------------------------------------------------------------------------

def test_bucket_stats_empty():
    s = _bucket_stats([])
    assert s["n"] == 0
    assert s["p50_ms"] == 0.0
    assert s["p95_ms"] == 0.0
    assert s["cost_usd"] == 0.0
    assert s["error_rate"] == 0.0
    assert s["errors"] == 0


def test_bucket_stats_basic(tmp_path):
    records = [
        {"latency_ms": 100, "cost_usd": 0.001, "ok": True},
        {"latency_ms": 300, "cost_usd": 0.002, "ok": True},
        {"latency_ms": 900, "cost_usd": 0.0,   "ok": False},
    ]
    s = _bucket_stats(records)
    assert s["n"] == 3
    assert s["p50_ms"] == 300.0
    assert s["errors"] == 1
    assert abs(s["error_rate"] - 1 / 3) < 0.001
    assert abs(s["cost_usd"] - 0.003) < 1e-9


def test_bucket_stats_error_rate_zero_when_all_ok():
    records = [{"latency_ms": 200, "cost_usd": 0.0, "ok": True}] * 5
    s = _bucket_stats(records)
    assert s["errors"] == 0
    assert s["error_rate"] == 0.0


# ---------------------------------------------------------------------------
# aggregate_turns — grouping + per-bucket stats
# ---------------------------------------------------------------------------

def test_aggregate_turns_empty():
    agg = aggregate_turns([])
    assert agg["total"]["n"] == 0
    assert agg["by_surface"] == {}
    assert agg["by_tier"] == {}


def test_aggregate_turns_by_surface(tmp_path):
    turns = [
        {"surface": "text",     "tier": "medium", "latency_ms": 100, "cost_usd": 0.0, "ok": True},
        {"surface": "text",     "tier": "medium", "latency_ms": 300, "cost_usd": 0.0, "ok": True},
        {"surface": "voice",    "tier": "local",  "latency_ms": 800, "cost_usd": 0.0, "ok": True},
        {"surface": "telegram", "tier": "medium", "latency_ms": 400, "cost_usd": 0.0, "ok": False},
    ]
    agg = aggregate_turns(turns)

    assert agg["total"]["n"] == 4
    assert agg["by_surface"]["text"]["n"] == 2
    assert agg["by_surface"]["text"]["p50_ms"] == 200.0
    assert agg["by_surface"]["voice"]["n"] == 1
    assert agg["by_surface"]["telegram"]["errors"] == 1
    assert agg["by_surface"]["telegram"]["error_rate"] == 1.0


def test_aggregate_turns_by_tier(tmp_path):
    turns = [
        {"surface": "text", "tier": "medium", "latency_ms": 200, "cost_usd": 0.001, "ok": True},
        {"surface": "text", "tier": "medium", "latency_ms": 400, "cost_usd": 0.001, "ok": True},
        {"surface": "text", "tier": "heavy",  "latency_ms": 800, "cost_usd": 0.01,  "ok": True},
    ]
    agg = aggregate_turns(turns)

    assert agg["by_tier"]["medium"]["n"] == 2
    assert agg["by_tier"]["medium"]["p50_ms"] == 300.0
    assert agg["by_tier"]["heavy"]["n"] == 1
    assert abs(agg["by_tier"]["medium"]["cost_usd"] - 0.002) < 1e-9


def test_aggregate_turns_p95_across_many():
    # 10 records: 9 × 100ms, 1 × 5000ms (the outlier).
    # With 10 sorted values, p95 → index 8.55 → between s[8]=100 and s[9]=5000.
    # p95 = 100 + (5000-100)*0.55 = 100 + 2695 = 2795ms — well above the cluster.
    turns = [{"surface": "text", "tier": "medium",
               "latency_ms": 100, "cost_usd": 0.0, "ok": True}] * 9
    turns.append({"surface": "text", "tier": "medium",
                  "latency_ms": 5000, "cost_usd": 0.0, "ok": True})
    agg = aggregate_turns(turns)
    assert agg["total"]["p50_ms"] == 100.0     # median sits in the cluster
    assert agg["total"]["p95_ms"] > 1000.0    # p95 is pulled toward the outlier


def test_aggregate_turns_by_surface_tier_key():
    turns = [
        {"surface": "text",  "tier": "medium", "latency_ms": 100, "cost_usd": 0.0, "ok": True},
        {"surface": "voice", "tier": "local",  "latency_ms": 900, "cost_usd": 0.0, "ok": True},
    ]
    agg = aggregate_turns(turns)
    assert "text:medium" in agg["by_surface_tier"]
    assert "voice:local" in agg["by_surface_tier"]


def test_aggregate_turns_error_rate(tmp_path):
    turns = [
        {"surface": "text", "tier": "medium", "latency_ms": 200, "cost_usd": 0.0, "ok": True},
        {"surface": "text", "tier": "medium", "latency_ms": 300, "cost_usd": 0.0, "ok": False},
        {"surface": "text", "tier": "medium", "latency_ms": 150, "cost_usd": 0.0, "ok": False},
    ]
    agg = aggregate_turns(turns)
    assert agg["total"]["errors"] == 2
    assert abs(agg["total"]["error_rate"] - 2 / 3) < 0.001


def test_aggregate_turns_unknown_surface():
    """Records with missing surface/tier gracefully land in '?' bucket."""
    turns = [{"latency_ms": 200, "cost_usd": 0.0, "ok": True}]
    agg = aggregate_turns(turns)
    assert "?" in agg["by_surface"] or "?" in agg["by_tier"]
