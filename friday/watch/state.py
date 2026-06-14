"""Mutable state + immutable config for the `sutra watch` loop.

Kept in its own module so the gating logic in `loop.py` stays small and so tests
can construct a `WatchState`/`WatchConfig` directly and assert on `decide_action`
without any I/O.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date

_DEFAULT_MODEL = "groq/meta-llama/llama-4-scout-17b-16e-instruct"


@dataclass(frozen=True)
class WatchConfig:
    """Tunables for the watch loop, all overridable via env (see `from_env`)."""

    poll_s: float = 20.0
    cooldown_s: float = 90.0
    change_threshold: int = 8
    daily_cap: int = 40
    speak: bool = False
    vision_model: str = _DEFAULT_MODEL

    @classmethod
    def from_env(cls) -> WatchConfig:
        """Read config from environment with the documented defaults."""
        return cls(
            poll_s=_env_float("WATCH_POLL_S", 20.0),
            cooldown_s=_env_float("WATCH_COOLDOWN_S", 90.0),
            change_threshold=_env_int("WATCH_CHANGE_THRESHOLD", 8),
            daily_cap=_env_int("WATCH_DAILY_CAP", 40),
            speak=_env_bool("WATCH_SPEAK", False),
            vision_model=os.getenv("WATCH_VISION_MODEL", _DEFAULT_MODEL),
        )


@dataclass
class WatchState:
    """Everything the loop remembers between ticks."""

    last_hash: int = 0
    last_suggest_ts: float = 0.0
    count_today: int = 0
    day: date = field(default_factory=date.today)
    recent: list[str] = field(default_factory=list)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
