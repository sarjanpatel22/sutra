"""LiteLLM tiered router with fallback chain, budget tracking, and alert callbacks."""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import litellm
from dotenv import load_dotenv

load_dotenv()

litellm.drop_params = True

_STATE_DIR = Path.home() / ".local" / "share" / "friday"
_BUDGET_FILE = _STATE_DIR / "budget.json"

ALERT_THRESHOLD = float(os.getenv("BUDGET_ALERT_THRESHOLD", "0.85"))


def allow_paid() -> bool:
    """Whether paid (Claude) tiers may be used at all. OFF by default — the $0
    guarantee. autoroute already honours this when picking a tier; the fallback
    chain in complete() must honour it too, or a rate-limited free tool turn would
    silently escalate to billed Claude behind the flag's back."""
    return os.getenv("ROUTER_ALLOW_PAID", "0").strip().lower() not in ("", "0", "false", "no")


class Tier(StrEnum):
    LOCAL = "local"
    LOCAL_TOOLS = "local_tools"  # local function-calling fallback (qwen2.5) — quota-proof
    REASON = "reason"  # local reasoning — Gemma 4 with thinking ON, free
    CODE = "code"  # local code model, loaded on demand (Phase 1.6 IDE bridge)
    MEDIUM = "medium"
    HEAVY = "heavy"
    MAX = "max"


TIER_MODELS: dict[Tier, list[str]] = {
    Tier.LOCAL: [
        os.getenv("ROUTER_LOCAL_MODEL", "ollama/gemma3:4b"),
    ],
    # Local FUNCTION-CALLING fallback. gemma3:4b (Tier.LOCAL) literally can't take a
    # tools array — Ollama rejects it ("does not support tools") — so when every free
    # CLOUD model is rate-limited, a tool turn would otherwise collapse to prose and
    # narrate an action that never happens. qwen2.5:7b DOES emit real tool_calls
    # locally (measured: scripts/probe_local_tools.py), so it's the offline safety net
    # that keeps actions working at $0 with no quota. Tried only on the tool-turn
    # fallback rung, after the cloud tiers — see _prefer_local_tools.
    Tier.LOCAL_TOOLS: [
        os.getenv("ROUTER_LOCAL_TOOLS_MODEL", "ollama_chat/qwen2.5:7b"),
    ],
    # Local reasoning: Gemma 4 (a reasoning model) with thinking left ON — slower
    # but it works through hard questions. Free/local; falls back to free Gemini.
    Tier.REASON: [
        os.getenv("ROUTER_REASON_MODEL", "ollama_chat/gemma4:e4b"),
        "gemini/gemini-2.5-flash",
    ],
    # Code tasks: qwen2.5:14b is already on disk — Ollama loads it on the first
    # CODE call and evicts it after its keep_alive idle window, so it costs zero
    # VRAM between code sessions. Free cloud fallback if Ollama is down.
    Tier.CODE: [
        os.getenv("ROUTER_CODE_MODEL", "ollama_chat/qwen2.5:14b"),
        "groq/llama-3.3-70b-versatile",
        "gemini/gemini-2.5-flash",
    ],
    # Free tier with headroom. Groq meters EACH model separately (≈100k tokens/day
    # per model), and tool turns carry big tool schemas — so one model exhausts fast.
    # Spread across several function-calling-capable Groq models (each its own quota)
    # plus Gemini, so a rate-limited model rolls to a FRESH one that can still call
    # tools, instead of collapsing to the local model (which can't function-call).
    Tier.MEDIUM: [
        # Stronger function-callers, each its own Groq quota bucket → better answers AND
        # more rate-limit headroom (one exhausting rolls to the next FRESH bucket). Kimi
        # isn't hosted on Groq, so these qwen3 models are the realistic "bigger brain" pick.
        "groq/qwen/qwen3-32b",
        "groq/qwen/qwen3.6-27b",
        "groq/llama-3.3-70b-versatile",
        "groq/openai/gpt-oss-120b",  # Groq's recommended replacement for the retired llama-4-scout
        "gemini/gemini-2.5-flash",
        "groq/llama-3.1-8b-instant",
        "groq/openai/gpt-oss-20b",
        "gemini/gemini-2.0-flash",
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

# Two usable top free brains (NVIDIA NIM, OpenAI-compatible via LiteLLM), inserted ABOVE the free
# fallbacks ONLY when the key is present — never thrashes when no key is set. LIVE-TESTED picks:
# GLM-5.1 is the stronger REASONER (leaderboard 83, reliable), Nemotron the faster FUNCTION-CALLER
# (1s tool calls). DeepSeek-V4-Pro and Kimi-K2.6 were rejected — they time out / return garbage on
# the free tier (rank ≠ usable). So GLM leads the reasoning tiers, Nemotron leads the tool/agent
# tier, and each backs the other. Override ids with NEMOTRON_MODEL / GLM_MODEL.
_NEMOTRON = os.getenv("NEMOTRON_MODEL", "nvidia_nim/nvidia/nemotron-3-super-120b-a12b")
_GLM = os.getenv("GLM_MODEL", "nvidia_nim/z-ai/glm-5.1")


def _wire_nvidia_nim(tier_models: dict[Tier, list[str]], glm: str, nemotron: str) -> None:
    """Insert the two free NVIDIA NIM brains into the free tiers IN PLACE, after the local/paid
    model at index 0. GLM leads the reasoning tiers (REASON/HEAVY); Nemotron leads the tool/agent
    tier (MEDIUM) for its ~1s tool calls, with GLM a reasoning-grade fallback behind it. A
    standalone function so the ordering is unit-testable without import-time env juggling."""
    for t in (Tier.REASON, Tier.HEAVY):           # reasoning tiers: GLM first, Nemotron behind it
        tier_models[t].insert(1, nemotron)
        tier_models[t].insert(1, glm)
    tier_models[Tier.MEDIUM].insert(2, nemotron)  # tool/agent tier: Nemotron first (1s calls)
    tier_models[Tier.MEDIUM].insert(3, glm)       # GLM as a reasoning-grade fallback


if os.getenv("NVIDIA_NIM_API_KEY"):
    _wire_nvidia_nim(TIER_MODELS, _GLM, _NEMOTRON)

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
_FREE_PREFIXES = ("ollama/", "ollama_chat/", "groq/", "gemini/", "nvidia_nim/")


def _cache_token_counts(usage: Any) -> tuple[int, int]:
    """(cache_read, cache_write) token counts from a litellm usage object — non-zero only
    for Anthropic prompt-cached calls. Used to bill cached tokens at their real (discounted)
    rate instead of full price, so the budget tracker matches the provider console."""
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return int(read), int(write)


# Rate-limit cooldown. When a free cloud model returns a 429/quota error, skip it for a
# while so the router routes STRAIGHT to a fresh (or local) model instead of re-hitting the
# exhausted one on every call. Module-level: persists across calls in a session, clears on
# restart. Local Ollama never 429s, so the chain always keeps a working final option.
_RATE_LIMITED: dict[str, float] = {}


def _rl_cooldown_s() -> float:
    """How long (seconds) to skip a model after it rate-limits. ROUTER_RATELIMIT_COOLDOWN."""
    try:
        return float(os.getenv("ROUTER_RATELIMIT_COOLDOWN", "120"))
    except ValueError:
        return 120.0


def _is_rate_limit_error(exc: Exception) -> bool:
    """True for a 429 / quota / rate-limit error (vs. a connection error, bad request, etc.)."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return (
        "ratelimit" in name
        or "429" in msg
        or "rate limit" in msg
        or "rate_limit" in msg
        or "quota" in msg
        or "too many requests" in msg
    )


def _mark_cooling_down(model: str) -> None:
    _RATE_LIMITED[model] = time.time() + _rl_cooldown_s()


def _is_cooling_down(model: str) -> bool:
    until = _RATE_LIMITED.get(model)
    if until is None:
        return False
    if time.time() >= until:
        _RATE_LIMITED.pop(model, None)  # cooldown elapsed — let the model back in
        return False
    return True

# Local models that cannot accept a tools array AT ALL — Ollama hard-rejects the
# request ("does not support tools"). Matched as a substring of the model id. Every
# OTHER local model (qwen2.5, llama3.x) emits real tool_calls, so it KEEPS its tools
# and can function-call offline. Verified empirically: scripts/probe_local_tools.py.
_LOCAL_NO_TOOLS = ("gemma",)


def _tool_incapable(model: str) -> bool:
    """True if `model` is a local model that can't accept tools (so they're stripped)."""
    if not model.startswith(("ollama/", "ollama_chat/")):
        return False  # cloud models all function-call
    return any(tag in model for tag in _LOCAL_NO_TOOLS)


def _prefer_local_tools(tiers: list[Tier]) -> list[Tier]:
    """For tool turns: try the tool-capable local model (qwen) BEFORE the chat-only
    local model (gemma), so a fully rate-limited tool turn still fires a real tool
    instead of degrading to prose that only *describes* the action."""
    out: list[Tier] = []
    for t in tiers:
        if t is Tier.LOCAL and Tier.LOCAL_TOOLS not in out:
            out.append(Tier.LOCAL_TOOLS)
        out.append(t)
    return out


def _local_tools_first() -> bool:
    """Prefer the local tool-capable model (qwen2.5:7b) for ACTION/tool turns, so they never
    rate-limit — $0 and quota-proof, with cloud (and Claude, if paid) as the fallback when the
    local model is down or fails. Default ON; set ROUTER_LOCAL_TOOLS_FIRST=0 to go back to
    cloud-first (faster and stronger, but subject to free-tier daily caps)."""
    val = os.getenv("ROUTER_LOCAL_TOOLS_FIRST", "1").strip().lower()
    return val not in ("", "0", "false", "no")


def _is_anthropic(model: str) -> bool:
    """True for Anthropic Claude models, which support prompt caching (`cache_control`).
    Other providers (Groq/Gemini/Ollama) don't understand it, so it's applied only here."""
    return "claude" in model.lower()


def _cache_enabled() -> bool:
    """Prompt caching on Claude is on by default; ROUTER_PROMPT_CACHE=0 disables it."""
    return os.getenv("ROUTER_PROMPT_CACHE", "1").strip().lower() not in ("", "0", "false", "no")


def _apply_cache_control(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """Mark the STATIC prefix — the system prompt and the tool schemas — with Anthropic
    `cache_control`, so a multi-round agentic turn re-reads them at ~10% cost instead of
    paying full price for the same ~50 tool schemas every round (the 16:1 input:output
    blow-up we measured). Anthropic-only; callers apply it solely when the target model is
    Claude. Returns NEW copies — the shared `messages`/`tools` (reused across the provider
    fallback loop) are never mutated."""
    new_messages: list[dict[str, Any]] = []
    marked_system = False
    for m in messages:
        if (not marked_system and m.get("role") == "system"
                and isinstance(m.get("content"), str) and m["content"]):
            new_messages.append({
                "role": "system",
                "content": [{
                    "type": "text",
                    "text": m["content"],
                    "cache_control": {"type": "ephemeral"},
                }],
            })
            marked_system = True
        else:
            new_messages.append(m)
    new_tools = tools
    if tools:
        # cache_control on the LAST tool tells Anthropic to cache the whole tools block.
        new_tools = [dict(t) for t in tools]
        new_tools[-1] = {**new_tools[-1], "cache_control": {"type": "ephemeral"}}
    return new_messages, new_tools

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

    def record(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> float:
        self._reset_day_if_needed()
        self._reset_month_if_needed()
        if model.startswith(_FREE_PREFIXES):
            cost_in, cost_out = 0.0, 0.0
        else:
            cost_in, cost_out = _COST_PER_1K.get(model, (0.005, 0.015))
        # Anthropic prompt caching: cached READS bill at ~10% and cache WRITES at 125%, the
        # rest at full rate. prompt_tokens is inclusive of both, so split it out — otherwise
        # the tracker over-bills a cached agentic turn vs. the real console spend.
        normal_in = max(0, input_tokens - cache_read - cache_write)
        cost = (
            normal_in * cost_in
            + cache_write * cost_in * 1.25
            + cache_read * cost_in * 0.10
            + output_tokens * cost_out
        ) / 1000
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
            cr, cw = _cache_token_counts(usage)
            _budget.record(
                model,
                getattr(usage, "prompt_tokens", 0),
                getattr(usage, "completion_tokens", 0),
                cache_read=cr,
                cache_write=cw,
            )
        yield chunk


# When every model in a tier fails (e.g. the free cloud tiers are rate-limited),
# fall through to these tiers in order so a turn degrades instead of hard-failing.
# Local Ollama is the universal last resort: free and never rate-limited.
_FALLBACK_TIERS: dict[Tier, list[Tier]] = {
    Tier.LOCAL: [],
    Tier.LOCAL_TOOLS: [Tier.LOCAL],  # qwen fails → gemma prose (last resort)
    Tier.REASON: [Tier.MEDIUM, Tier.LOCAL],
    Tier.CODE: [Tier.MEDIUM, Tier.LOCAL],
    # qwen before gemma: it's the stronger writer, so even a no-tools synthesis after
    # a cloud outage reads naturally instead of gemma parroting the tool-call history.
    Tier.MEDIUM: [Tier.LOCAL_TOOLS, Tier.LOCAL],
    Tier.HEAVY: [Tier.MEDIUM, Tier.LOCAL],
    Tier.MAX: [Tier.HEAVY, Tier.MEDIUM, Tier.LOCAL],
}


async def complete(
    messages: list[dict[str, Any]],
    tier: Tier = Tier.HEAVY,
    *,
    stream: bool = False,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    tools: list[dict[str, Any]] | None = None,
    free_only: bool = False,
    local_tools_first: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Route a completion through the tier's model chain with automatic fallback.

    On total tier failure (all models error — typically the free tiers being rate-
    limited) it falls through to _FALLBACK_TIERS so the turn still completes. A turn
    that needs tools and hits a rate-limited free tier escalates to Claude (reliable
    function-calling, budget-permitting) before the weaker local model.

    local_tools_first overrides the ROUTER_LOCAL_TOOLS_FIRST default for this call:
    None = use the env default, False = go cloud-first (used for high-effort/big tasks,
    which the small local model can't carry), True = force local-first.
    """
    primary = tier
    # The $0 chokepoint: a paid tier (HEAVY/MAX) drops to the free MEDIUM tier UNLESS paid
    # is explicitly enabled AND affordable AND not a free_only worker. This enforces the
    # ROUTER_ALLOW_PAID guarantee here, structurally — not just by trusting every caller to
    # pre-pick the tier. So even `ask --tier heavy` or run_autonomous's HEAVY default stays
    # $0 until ROUTER_ALLOW_PAID=1. (The fallback escalation below already checks allow_paid.)
    if tier in (Tier.HEAVY, Tier.MAX) and (
        free_only or not allow_paid() or not _budget.can_afford(tier)
    ):
        primary = Tier.MEDIUM

    fallback = list(_FALLBACK_TIERS.get(primary, []))
    if (tools and primary == Tier.MEDIUM and not free_only
            and allow_paid() and _budget.can_afford(Tier.HEAVY)):
        fallback = [Tier.HEAVY, *fallback]  # tool turn + rate limit → Claude (only if paid allowed)
    if tools:
        # ...then a tool-CAPABLE local model (qwen) before the chat-only one (gemma),
        # so a fully rate-limited tool turn still acts instead of just narrating.
        fallback = _prefer_local_tools(fallback)

    tier_order = [primary, *fallback]
    # Local-first for ACTION turns: try the quota-proof local tool model (qwen2.5:7b) BEFORE
    # the free cloud tier, so actions never hit a daily rate cap. Cloud (and Claude, if paid)
    # remain as fallbacks when the local model is down or fails. Default on — see
    # _local_tools_first (ROUTER_LOCAL_TOOLS_FIRST=0 restores cloud-first).
    use_local_first = _local_tools_first() if local_tools_first is None else local_tools_first
    if tools and primary == Tier.MEDIUM and use_local_first:
        tier_order = [Tier.LOCAL_TOOLS, *tier_order]

    models: list[str] = []
    for tr in tier_order:
        for m in TIER_MODELS[tr]:
            if m not in models:
                models.append(m)

    # Skip models cooling down from a recent 429 → go straight to a fresh/local one instead of
    # eating the round-trip + 429 again. Local never rate-limits, so something is always left;
    # if somehow ALL are cooling down, try the full list anyway rather than failing outright.
    attempt = [m for m in models if not _is_cooling_down(m)] or models

    last_err: Exception | None = None
    for model in attempt:
        # Strip tools only for local models that genuinely can't accept them (gemma —
        # Ollama rejects the request outright). qwen2.5/llama KEEP their tools and
        # function-call locally, which is what makes the offline fallback actually act.
        call_tools = None if _tool_incapable(model) else tools
        call_messages = messages
        # Prompt caching (Claude only): cache the static system+tools prefix so an agentic
        # multi-round turn doesn't re-pay full price for the same ~50 schemas every round.
        if _is_anthropic(model) and _cache_enabled():
            call_messages, call_tools = _apply_cache_control(messages, call_tools)
        try:
            call_kwargs = dict(kwargs)
            if call_tools is None:
                call_kwargs.pop("tool_choice", None)  # no tools → can't force a tool call
            if stream:
                call_kwargs.setdefault("stream_options", {"include_usage": True})
            response = await litellm.acompletion(
                model=model,
                messages=call_messages,
                stream=stream,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=call_tools,
                **call_kwargs,
            )
            if stream:
                return _tracked_stream(response, model)
            usage = getattr(response, "usage", None)
            if usage:
                cr, cw = _cache_token_counts(usage)
                _budget.record(
                    model,
                    getattr(usage, "prompt_tokens", 0),
                    getattr(usage, "completion_tokens", 0),
                    cache_read=cr,
                    cache_write=cw,
                )
            return response
        except Exception as e:  # noqa: BLE001
            if _is_rate_limit_error(e):
                _mark_cooling_down(model)  # skip this model on the next calls until it cools
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
