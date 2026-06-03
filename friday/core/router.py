"""LiteLLM tiered router with fallback chain, budget tracking, and alert callbacks."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator

import litellm
from dotenv import load_dotenv

load_dotenv()

litellm.drop_params = True

_STATE_DIR = Path.home() / ".local" / "share" / "friday"
_BUDGET_FILE = _STATE_DIR / "budget.json"

ALERT_THRESHOLD = float(os.getenv("BUDGET_ALERT_THRESHOLD", "0.85"))


class Tier(str, Enum):
    LOCAL = "local"
    MEDIUM = "medium"
    HEAVY = "heavy"
    MAX = "max"


TIER_MODELS: dict[Tier, list[str]] = {
    Tier.LOCAL: [
        os.getenv("ROUTER_LOCAL_MODEL", "ollama/gemma3:4b"),
    ],
    Tier.MEDIUM: [
        "groq/llama-3.3-70b-versatile",
        "gemini/gemini-2.5-flash",
    ],
    Tier.HEAVY: [
        os.getenv("ROUTER_CLOUD_HEAVY", "claude-sonnet-4-6"),
        "groq/llama-3.3-70b-versatile",
    ],
    Tier.MAX: [
        os.getenv("ROUTER_CLOUD_MAX", "claude-opus-4-8"),
        os.getenv("ROUTER_CLOUD_HEAVY", "claude-sonnet-4-6"),
    ],
}

_COST_PER_1K: dict[str, tuple[float, float]] = {
    "claude-opus-4-8":                       (0.015, 0.075),
    "claude-sonnet-4-6":                     (0.003, 0.015),
    "claude-haiku-4-5":                      (0.0008, 0.004),
    "groq/llama-3.3-70b-versatile":          (0.0, 0.0),
    "gemini/gemini-2.5-flash": (0.0, 0.0),
    "ollama/gemma3:4b":                      (0.0, 0.0),
}

# Provider prefixes that are always free (local or free-tier APIs).
# Guards against silently charging the default rate on a model-name typo.
_FREE_PREFIXES = ("ollama/", "groq/", "gemini/")

# Registered alert callbacks: fn(scope, pct_used) where scope is "daily"|"monthly"
_alert_callbacks: list[Callable[[str, float], None]] = []


def register_alert(fn: Callable[[str, float], None]) -> None:
    """Register a callback invoked when spend crosses ALERT_THRESHOLD."""
    _alert_callbacks.append(fn)


def _fire_alerts(scope: str, pct: float) -> None:
    for cb in _alert_callbacks:
        try:
            cb(scope, pct)
        except Exception:  # noqa: BLE001
            pass


@dataclass
class BudgetTracker:
    daily_limit: float = float(os.getenv("DAILY_BUDGET_USD", "2.0"))
    monthly_limit: float = float(os.getenv("MONTHLY_BUDGET_USD", "30.0"))
    _daily_spent: float = field(default=0.0, init=False)
    _monthly_spent: float = field(default=0.0, init=False)
    _daily_tokens: int = field(default=0, init=False)
    _monthly_tokens: int = field(default=0, init=False)
    _day_start: float = field(default_factory=time.time, init=False)
    _month_start: float = field(default_factory=time.time, init=False)
    # Track whether alert has been fired this period to avoid spam
    _daily_alert_fired: bool = field(default=False, init=False)
    _monthly_alert_fired: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._load()

    def _load(self) -> None:
        if not _BUDGET_FILE.exists():
            return
        try:
            data = json.loads(_BUDGET_FILE.read_text())
            now = time.time()
            # Restore daily if same calendar day
            if now - data.get("day_start", 0) < 86400:
                self._daily_spent = data.get("daily_spent", 0.0)
                self._daily_tokens = data.get("daily_tokens", 0)
                self._day_start = data.get("day_start", now)
                self._daily_alert_fired = data.get("daily_alert_fired", False)
            # Restore monthly if same 30-day window
            if now - data.get("month_start", 0) < 30 * 86400:
                self._monthly_spent = data.get("monthly_spent", 0.0)
                self._monthly_tokens = data.get("monthly_tokens", 0)
                self._month_start = data.get("month_start", now)
                self._monthly_alert_fired = data.get("monthly_alert_fired", False)
        except Exception:  # noqa: BLE001
            pass

    def _save(self) -> None:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _BUDGET_FILE.write_text(json.dumps({
            "daily_spent": self._daily_spent,
            "daily_tokens": self._daily_tokens,
            "monthly_spent": self._monthly_spent,
            "monthly_tokens": self._monthly_tokens,
            "day_start": self._day_start,
            "month_start": self._month_start,
            "daily_alert_fired": self._daily_alert_fired,
            "monthly_alert_fired": self._monthly_alert_fired,
        }, indent=2))

    def _reset_day_if_needed(self) -> None:
        if time.time() - self._day_start > 86400:
            self._daily_spent = 0.0
            self._daily_tokens = 0
            self._day_start = time.time()
            self._daily_alert_fired = False

    def _reset_month_if_needed(self) -> None:
        if time.time() - self._month_start > 30 * 86400:
            self._monthly_spent = 0.0
            self._monthly_tokens = 0
            self._month_start = time.time()
            self._monthly_alert_fired = False

    def record(self, model: str, input_tokens: int, output_tokens: int) -> float:
        self._reset_day_if_needed()
        self._reset_month_if_needed()
        if model.startswith(_FREE_PREFIXES):
            cost_in, cost_out = 0.0, 0.0
        else:
            cost_in, cost_out = _COST_PER_1K.get(model, (0.005, 0.015))
        cost = (input_tokens / 1000) * cost_in + (output_tokens / 1000) * cost_out
        total_tokens = input_tokens + output_tokens
        self._daily_spent += cost
        self._monthly_spent += cost
        self._daily_tokens += total_tokens
        self._monthly_tokens += total_tokens
        self._check_thresholds()
        self._save()
        return cost

    def _check_thresholds(self) -> None:
        if self.daily_limit > 0:
            daily_pct = self._daily_spent / self.daily_limit
            if daily_pct >= ALERT_THRESHOLD and not self._daily_alert_fired:
                self._daily_alert_fired = True
                _fire_alerts("daily", daily_pct)
        if self.monthly_limit > 0:
            monthly_pct = self._monthly_spent / self.monthly_limit
            if monthly_pct >= ALERT_THRESHOLD and not self._monthly_alert_fired:
                self._monthly_alert_fired = True
                _fire_alerts("monthly", monthly_pct)

    def can_afford(self, tier: Tier) -> bool:
        self._reset_day_if_needed()
        self._reset_month_if_needed()
        if self._daily_spent >= self.daily_limit:
            return False
        if self._monthly_spent >= self.monthly_limit:
            return False
        return True

    @property
    def daily_spent(self) -> float:
        return self._daily_spent

    @property
    def monthly_spent(self) -> float:
        return self._monthly_spent

    @property
    def daily_tokens(self) -> int:
        return self._daily_tokens

    @property
    def monthly_tokens(self) -> int:
        return self._monthly_tokens

    @property
    def daily_pct(self) -> float:
        return (self._daily_spent / self.daily_limit * 100) if self.daily_limit else 0.0

    @property
    def monthly_pct(self) -> float:
        return (self._monthly_spent / self.monthly_limit * 100) if self.monthly_limit else 0.0


_budget = BudgetTracker()


def get_budget() -> BudgetTracker:
    return _budget


async def _tracked_stream(response: Any, model: str) -> AsyncIterator[Any]:
    """Re-yield a streaming response, recording budget from the usage chunk.

    With stream_options={"include_usage": True}, the final chunk carries token
    usage (empty choices). Providers that ignore the option (e.g. Ollama) simply
    never emit it — fine, since those tiers are free anyway.
    """
    async for chunk in response:
        usage = getattr(chunk, "usage", None)
        if usage:
            _budget.record(
                model,
                getattr(usage, "prompt_tokens", 0),
                getattr(usage, "completion_tokens", 0),
            )
        yield chunk


async def complete(
    messages: list[dict[str, Any]],
    tier: Tier = Tier.HEAVY,
    *,
    stream: bool = False,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    tools: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> Any:
    """Route a completion through the tier's model chain with automatic fallback."""
    models = list(TIER_MODELS[tier])

    if tier in (Tier.HEAVY, Tier.MAX) and not _budget.can_afford(tier):
        models = list(TIER_MODELS[Tier.MEDIUM])

    last_err: Exception | None = None
    for model in models:
        try:
            call_kwargs = dict(kwargs)
            if stream:
                call_kwargs.setdefault("stream_options", {"include_usage": True})
            response = await litellm.acompletion(
                model=model,
                messages=messages,
                stream=stream,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
                **call_kwargs,
            )
            if stream:
                return _tracked_stream(response, model)
            usage = getattr(response, "usage", None)
            if usage:
                _budget.record(
                    model,
                    getattr(usage, "prompt_tokens", 0),
                    getattr(usage, "completion_tokens", 0),
                )
            return response
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue

    raise RuntimeError(
        f"All models in tier {tier.value} failed. Last error: {last_err}"
    ) from last_err


async def stream_complete(
    messages: list[dict[str, Any]],
    tier: Tier = Tier.HEAVY,
    **kwargs: Any,
) -> AsyncIterator[str]:
    """Yield text chunks from a streaming completion."""
    response = await complete(messages, tier=tier, stream=True, **kwargs)
    async for chunk in response:
        if not chunk.choices:  # usage-only final chunk has empty choices
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            yield delta.content
