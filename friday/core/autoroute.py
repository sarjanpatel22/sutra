"""Automatic tier + effort selection — Sutra decides, per turn, free by default.

One brain classifies every turn into an **effort** and routes it:

  low    — a greeting, an opinion, chat. No tools; ride the free local/medium path.
  medium — needs one live fact / a single tool (time, weather, search, memory, a
           file, an OS stat). Act, but it's light → free MEDIUM tier (Groq/Gemini).
  high   — a real task: a web flow, coding, deep analysis, anything multi-step or
           long. Act with full tools → ideally Claude, but that's paid, so by
           default high stays free too (coding → local code model, else Groq-70B).

`decide()` returns whether to *act* (go agentic with tools) **and** at what effort,
so the voice and chat paths share one judgment instead of a keyword whitelist.

Set ROUTER_ALLOW_PAID=1 (+ a working ANTHROPIC_API_KEY) to let high turns use
Claude. Until then this guarantees $0 — nothing here ever selects the paid tier.
Pattern-based, no extra LLM call in the hot path.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from friday.core.router import Tier, get_budget

# A URL almost always means a real web task → wants the strongest reasoning available.
_URL = re.compile(r"https?://|\bwww\.|\b[a-z0-9-]+\.(?:com|org|net|io|dev|ai|co|gov|edu)\b", re.I)

# Strong signals a turn is hard/agentic (word-boundary matched so "plan" doesn't fire
# inside "explanation"). Broad question words are excluded to avoid over-escalating.
_HEAVY = re.compile(
    r"\b("
    r"browse|web ?site|web ?task|navigate|go to|open \w|launch|"
    r"log ?in|sign ?in|fill (?:in|out)|submit|book|order|purchase|checkout|automate|scrape|"
    r"start (?:a |the )?(?:room|lab|machine|box)|deploy|"
    r"pick (?:a |an |the )?(?:room|lab|machine|box|target|challenge)|"
    r"tryhackme|hack ?the ?box|pwn|exploit|enumerate|recon|nmap|\bscan\b|"
    r"analy[sz]e|compare|evaluate|assess|research|investigate|summari[sz]e|"
    r"design|architect|strateg|draft|compose|"
    r"debug|refactor|optimi[sz]e|implement|traceback|stack ?trace|"
    r"step by step|and then|after that|walk me through|figure out|work out"
    r")\b",
    re.I,
)

# Coding intent → the code tier (free local qwen, groq fallback).
_CODE = re.compile(
    r"\b(write (?:me )?(?:a |some )?(?:code|script|function|program)|unit test|pytest|"
    r"git (?:commit|diff|status|push)|pull request|refactor|stack ?trace|code review|"
    r"fix (?:the|this|my) (?:bug|code|function|error))\b",
    re.I,
)

# Imperative/request action verbs — robust to natural speech & STT spacing (e.g.
# "can you open the try hack me"). Liberal on purpose: a false positive just routes
# a chat turn to the (free) tool-capable agent, which simply answers — cheap. Missing
# a real action (the old failure) is the costly mistake, so we bias toward acting.
_ACTION = re.compile(
    r"\b("
    r"open|launch|start (?!over\b)|show me|pull up|bring up|go to|navigate|visit|"
    r"play|run|click|type|fill|book|order|send|download|install|sign up|register|"
    r"find me|get me|look up|search|browse|deploy|connect to"
    r")\b",
    re.I,
)

# Light tool needs: one live fact / a single action. Worth acting on, but not "high".
# (Folds in what used to be agent.likely_needs_tool so voice no longer needs it.)
_NEEDS_TOOL = re.compile(
    r"\b("
    r"what(?:'s| is)? the time|what time|what day|what(?:'s| is)? (?:the )?date|today'?s date|"
    r"weather|temperature|forecast|how hot|how cold|raining|"
    r"how much (?:ram|memory|storage|disk)|cpu|disk space|gpu|battery|system stat|"
    r"calculate|square root|percent of|plus|minus|times|divided by|multiplied|"
    r"search (?:for|the)|look up|google|latest news|current price|who won|"
    r"clipboard|what did i copy|"
    r"what do you know about|do you remember|recall|remind me"
    r")\b",
    re.I,
)


@dataclass(frozen=True)
class Decision:
    """One turn's verdict: act-with-tools or just chat, at what effort, and why."""

    act: bool          # go agentic (use skills) vs a plain conversational reply
    tier: Tier         # which tier to run on
    effort: str        # "low" | "medium" | "high" — for the UI/feed
    reason: str        # one-line human explanation


def _allow_paid() -> bool:
    """Whether high turns may use the paid Claude tier (default off → always free)."""
    return os.getenv("ROUTER_ALLOW_PAID", "0").strip().lower() not in ("", "0", "false", "no")


def _paid_ok() -> bool:
    """Paid allowed *and* budget not blown (over-budget silently behaves as free-only)."""
    if not _allow_paid():
        return False
    try:
        b = get_budget()
        if b.daily_pct >= 100 or b.monthly_pct >= 100:
            return False
    except Exception:  # noqa: BLE001 — budget unavailable; assume ok, never crash routing
        pass
    return True


def decide(text: str, history: list[dict[str, Any]] | None = None) -> Decision:
    """Single judgment for a turn — used by both voice and chat."""
    t = (text or "").strip()
    paid = _paid_ok()

    coding = bool(_CODE.search(t))
    action = bool(_ACTION.search(t))
    high = bool(
        _URL.search(t) or _HEAVY.search(t) or coding or action
        or len(t.split()) > 40 or len(t) > 240
    )

    if high:
        why = (
            "web task" if _URL.search(t)
            else "coding" if coding
            else "action" if action
            else "complex / multi-step"
        )
        if paid:
            return Decision(True, Tier.HEAVY, "high", f"{why} → Claude")
        if coding:
            return Decision(True, Tier.CODE, "high", f"{why} → free local code model")
        return Decision(True, Tier.MEDIUM, "high", f"{why} → free 70B")

    if _NEEDS_TOOL.search(t):
        return Decision(True, Tier.MEDIUM, "medium", "needs a tool → free tier")

    # Plain chat — no tools, no cost.
    return Decision(False, Tier.MEDIUM, "low", "just talking → free")


def route(text: str, history: list[dict[str, Any]] | None = None) -> tuple[Tier, str]:
    """(tier, one-line reason) — back-compat shim over decide() for the dashboard."""
    d = decide(text, history)
    return d.tier, d.reason


def pick_tier(text: str, history: list[dict[str, Any]] | None = None) -> Tier:
    """Just the tier for a turn (free by default; Claude only if ROUTER_ALLOW_PAID)."""
    return decide(text, history).tier
