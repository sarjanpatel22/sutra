"""Aggregate the turn-latency logs into a shakedown dashboard: p50/p95 + cost.

The voice path writes ``voice_latency.jsonl`` and the text path writes
``turn_latency.jsonl`` (see ``turnlog``). This reads both and computes per-stage /
per-tier percentiles plus today's rolling spend — the numbers Gate 2 ("are you
actually using it daily, and is it fast enough?") is judged on. Pure stdlib; no
numpy, and it never imports the heavy voice module.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from friday.core.turnlog import TEXT_LATENCY_LOG

VOICE_LATENCY_LOG = Path.home() / ".local" / "share" / "friday" / "voice_latency.jsonl"


def _load(path: Path) -> list[dict[str, Any]]:
    """Read a jsonl file into a list of dicts; tolerant of blanks/corruption."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (p in 0..100). Returns 0.0 for empty input."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def _metric(records: list[dict[str, Any]], key: str) -> dict[str, float]:
    vals = [r[key] for r in records if isinstance(r.get(key), (int, float))]
    return {"n": len(vals), "p50": percentile(vals, 50), "p95": percentile(vals, 95)}


def gather_stats() -> dict[str, Any]:
    """Build the full stats report from both latency logs + the budget tracker."""
    text = _load(TEXT_LATENCY_LOG)
    voice = _load(VOICE_LATENCY_LOG)

    by_tier: dict[str, list[dict[str, Any]]] = {}
    for r in text:
        by_tier.setdefault(str(r.get("tier", "?")), []).append(r)
    text_by_tier = {
        tier: {
            **_metric(recs, "total_ms"),
            "tokens": sum(
                (r.get("in_tokens", 0) or 0) + (r.get("out_tokens", 0) or 0) for r in recs
            ),
            "avg_rounds": (
                sum(r.get("tool_rounds", 0) or 0 for r in recs) / len(recs) if recs else 0.0
            ),
        }
        for tier, recs in sorted(by_tier.items())
    }

    voice_stages = {
        stage: _metric(voice, stage)
        for stage in ("stt_ms", "agent_ttft_ms", "tts_first_ms", "total_ms")
    }

    from friday.core.router import get_budget

    b = get_budget()
    return {
        "text_turns": len(text),
        "voice_turns": len(voice),
        "text_by_tier": text_by_tier,
        "voice_stages": voice_stages,
        "budget": {
            "daily_spent": b.daily_spent,
            "daily_limit": b.daily_limit,
            "monthly_spent": b.monthly_spent,
            "monthly_limit": b.monthly_limit,
            "daily_tokens": b.daily_tokens,
        },
    }
