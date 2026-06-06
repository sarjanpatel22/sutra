"""Per-turn latency/cost log for the text agent path.

Gate 2 (the daily-driver shakedown) needs numbers. The voice path already logs to
``voice_latency.jsonl``; this is its text-turn equivalent, written by ``agent.run``
and read back by ``sutra stats``. Logging must never break a turn, so every write
is best-effort.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

TEXT_LATENCY_LOG = Path.home() / ".local" / "share" / "friday" / "turn_latency.jsonl"


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
    """Append one text-turn record. Swallows all errors — observability is never fatal."""
    try:
        TEXT_LATENCY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(TEXT_LATENCY_LOG, "a") as f:
            f.write(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "tier": tier,
                        "model": model,
                        "tool_rounds": tool_rounds,
                        "total_ms": round(total_ms),
                        "in_tokens": in_tokens,
                        "out_tokens": out_tokens,
                        "preview": preview[:80],
                    }
                )
                + "\n"
            )
    except Exception:  # noqa: BLE001 — a logging failure must never break a reply
        pass
