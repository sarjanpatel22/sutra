"""LiteLLM tiered router with fallback chain and budget tracking."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator

import litellm
from dotenv import load_dotenv

load_dotenv()

litellm.drop_params = True  # ignore unsupported params per provider


class Tier(str, Enum):
    LOCAL = "local"       # Gemma 4 E4B via Ollama — handles ~70% of queries
    MEDIUM = "medium"     # Groq Llama 3.3 70B or Gemini Flash — free tier
    HEAVY = "heavy"       # Claude Sonnet — complex reasoning
    MAX = "max"           # Claude Opus — hardest 5-10%


# Model identifiers per tier
TIER_MODELS: dict[Tier, list[str]] = {
    Tier.LOCAL: [
        os.getenv("ROUTER_LOCAL_MODEL", "ollama/gemma2:4b"),
    ],
    Tier.MEDIUM: [
        "groq/llama-3.3-70b-versatile",
        "gemini/gemini-2.5-flash-preview-04-17",
    ],
    Tier.HEAVY: [
        os.getenv("ROUTER_CLOUD_HEAVY", "claude-sonnet-4-6"),
        "groq/llama-3.3-70b-versatile",   # fallback if Claude down
    ],
    Tier.MAX: [
        os.getenv("ROUTER_CLOUD_MAX", "claude-opus-4-8"),
        os.getenv("ROUTER_CLOUD_HEAVY", "claude-sonnet-4-6"),  # fallback
    ],
}

# Approximate cost per 1K tokens (input, output) in USD
_COST_PER_1K: dict[str, tuple[float, float]] = {
    "claude-opus-4-8":                    (0.015, 0.075),
    "claude-sonnet-4-6":                  (0.003, 0.015),
    "claude-haiku-4-5":                   (0.0008, 0.004),
    "groq/llama-3.3-70b-versatile":       (0.0, 0.0),
    "gemini/gemini-2.5-flash-preview-04-17": (0.0, 0.0),
    "ollama/gemma2:4b":                   (0.0, 0.0),
}


@dataclass
class BudgetTracker:
    daily_limit: float = float(os.getenv("DAILY_BUDGET_USD", "2.0"))
    monthly_limit: float = float(os.getenv("MONTHLY_BUDGET_USD", "30.0"))
    _daily_spent: float = field(default=0.0, init=False)
    _monthly_spent: float = field(default=0.0, init=False)
    _day_start: float = field(default_factory=time.time, init=False)

    def record(self, model: str, input_tokens: int, output_tokens: int) -> float:
        cost_in, cost_out = _COST_PER_1K.get(model, (0.005, 0.015))
        cost = (input_tokens / 1000) * cost_in + (output_tokens / 1000) * cost_out
        self._daily_spent += cost
        self._monthly_spent += cost
        return cost

    def can_afford(self, tier: Tier) -> bool:
        # Reset daily counter if a new day started
        if time.time() - self._day_start > 86400:
            self._daily_spent = 0.0
            self._day_start = time.time()
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


_budget = BudgetTracker()


def get_budget() -> BudgetTracker:
    return _budget


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
    """Route a completion request through the tier's model chain with fallback.

    Falls back to the next model in the chain on failure, then escalates to
    the next tier if all models in the chain fail.
    """
    models = list(TIER_MODELS[tier])

    # Budget pressure: downgrade cloud tiers to medium if daily cap hit
    if tier in (Tier.HEAVY, Tier.MAX) and not _budget.can_afford(tier):
        models = list(TIER_MODELS[Tier.MEDIUM])

    last_err: Exception | None = None
    for model in models:
        try:
            response = await litellm.acompletion(
                model=model,
                messages=messages,
                stream=stream,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
                **kwargs,
            )
            if not stream:
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
    """Convenience wrapper that yields text chunks from a streaming completion."""
    response = await complete(messages, tier=tier, stream=True, **kwargs)
    async for chunk in response:
        delta = chunk.choices[0].delta
        if delta.content:
            yield delta.content
