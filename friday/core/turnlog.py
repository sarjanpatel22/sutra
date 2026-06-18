"""Per-turn structured event log for all agent surfaces.

Every completed agent turn — text, voice, or Telegram — appends one JSON object
to ``turns.jsonl``.  The text path also appends the narrower latency record to
``turn_latency.jsonl`` (kept for backwards-compat; ``sutra stats`` reads it).
``sutra logs`` reads ``turns.jsonl`` for the richer per-surface / per-tier view.

Design constraints:
- Pure stdlib (no heavy deps). OTel/Wazuh are follow-ons (see docs/SUTRA_PHASES.md).
- Every write is best-effort: a logging failure MUST NEVER break a turn.
- All public functions are synchronous + pure (testable with tmp_path, no network).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

# Legacy narrow log (read by friday.core.stats / sutra stats). Kept for compat.
TEXT_LATENCY_LOG = Path.home() / ".local" / "share" / "friday" / "turn_latency.jsonl"

# Rich structured log — one record per turn across all surfaces.
TURNS_LOG = Path.home() / ".local" / "share" / "friday" / "turns.jsonl"

# Keep append-only logs bounded so they can't grow without limit over months of use.
_MAX_LOG_LINES = 5000


def append_capped(path: Path, line: str, max_lines: int = _MAX_LOG_LINES) -> None:
    """Append `line` to `path`, then keep only its last `max_lines` lines.

    Bounds an append-only log so it can't grow forever. Writes here are infrequent (per
    turn / per engagement) and the files small, so the rewrite cost is negligible.
    Best-effort: a logging failure must never break the caller.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = path.read_text().splitlines() if path.exists() else []
        lines.append(line.rstrip("\n"))
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        path.write_text("\n".join(lines) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def log_text_turn(
    *,
    tier: str,
    model: str,
    tool_rounds: int,
    total_ms: float,
    in_tokens: int,
    out_tokens: int,
    preview: str = "",
) -> None:
    """Append one text-turn record to the legacy latency log.

    Kept for backward-compat with ``sutra stats`` / ``friday.core.stats``.
    Swallows all errors — observability is never fatal.
    """
    try:
        append_capped(TEXT_LATENCY_LOG, json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tier": tier,
            "model": model,
            "tool_rounds": tool_rounds,
            "total_ms": round(total_ms),
            "in_tokens": in_tokens,
            "out_tokens": out_tokens,
            "preview": preview[:80],
        }))
    except Exception:  # noqa: BLE001 — a logging failure must never break a reply
        pass


def log_turn(
    *,
    surface: str,
    tier: str,
    effort: str = "",
    tool_calls: list[str] | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    latency_ms: float = 0.0,
    ok: bool = True,
    error: str = "",
    model: str = "",
) -> None:
    """Append one structured turn record to ``turns.jsonl``.

    Args:
        surface:    "text" | "voice" | "telegram" (or any future surface).
        tier:       Router tier value, e.g. "medium", "heavy", "local".
        effort:     Autoroute effort label, e.g. "act", "chat", "" if unknown.
        tool_calls: Names of tools called this turn (empty list = no tools).
        tokens_in:  Prompt tokens consumed.
        tokens_out: Completion tokens generated.
        cost_usd:   USD cost for this turn (0.0 for free-tier turns).
        latency_ms: Total wall-clock latency in milliseconds.
        ok:         False if the turn raised an unhandled exception.
        error:      Error message if ok=False.
        model:      Resolved model id (e.g. "groq/llama-3.3-70b-versatile").

    Swallows all I/O errors — observability must never break a turn.
    """
    try:
        TURNS_LOG.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "surface": surface,
            "tier": tier,
            "effort": effort,
            "tool_calls": tool_calls if tool_calls is not None else [],
            "tool_count": len(tool_calls) if tool_calls is not None else 0,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": round(cost_usd, 6),
            "latency_ms": round(latency_ms),
            "ok": ok,
            "error": error[:200] if error else "",
            "model": model,
        }
        append_capped(TURNS_LOG, json.dumps(record))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Readers + aggregation  (pure functions — testable with tmp_path)
# ---------------------------------------------------------------------------

def load_turns(path: Path | None = None) -> list[dict[str, Any]]:
    """Load turns.jsonl (or a custom path). Tolerant of blanks / corruption."""
    p = path or TURNS_LOG
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _pct(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (p in 0..100). Returns 0.0 on empty."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def _bucket_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute aggregate stats for a slice of turn records."""
    n = len(records)
    if n == 0:
        return {"n": 0, "p50_ms": 0.0, "p95_ms": 0.0,
                "cost_usd": 0.0, "error_rate": 0.0, "errors": 0}
    latencies = [r["latency_ms"] for r in records
                 if isinstance(r.get("latency_ms"), (int, float))]
    errors = sum(1 for r in records if not r.get("ok", True))
    return {
        "n": n,
        "p50_ms": round(_pct(latencies, 50), 1),
        "p95_ms": round(_pct(latencies, 95), 1),
        "cost_usd": round(sum(r.get("cost_usd", 0.0) or 0.0 for r in records), 6),
        "error_rate": round(errors / n, 4),
        "errors": errors,
    }


def aggregate_turns(
    turns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Group turns by surface and tier, compute p50/p95/cost/error_rate.

    Returns a dict with keys:
      - "total":   overall stats across all turns
      - "by_surface":  { surface: stats }
      - "by_tier":     { tier: stats }
      - "by_surface_tier": { "surface:tier": stats }
    """
    by_surface: dict[str, list[dict[str, Any]]] = {}
    by_tier: dict[str, list[dict[str, Any]]] = {}
    by_surface_tier: dict[str, list[dict[str, Any]]] = {}

    for r in turns:
        surface = str(r.get("surface", "?"))
        tier = str(r.get("tier", "?"))
        key = f"{surface}:{tier}"
        by_surface.setdefault(surface, []).append(r)
        by_tier.setdefault(tier, []).append(r)
        by_surface_tier.setdefault(key, []).append(r)

    return {
        "total": _bucket_stats(turns),
        "by_surface": {k: _bucket_stats(v) for k, v in sorted(by_surface.items())},
        "by_tier": {k: _bucket_stats(v) for k, v in sorted(by_tier.items())},
        "by_surface_tier": {k: _bucket_stats(v) for k, v in sorted(by_surface_tier.items())},
    }
