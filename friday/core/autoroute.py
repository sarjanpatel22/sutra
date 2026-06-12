"""Automatic tier selection — Sutra picks the right tier per turn, free by default.

She classifies each turn's weight and routes accordingly. Simple turns (a fact, a
greeting, one quick tool call) ride the free MEDIUM tier (Groq/Gemini, $0). Heavier
turns — web tasks, analysis, coding, anything long — would ideally go to Claude, but
that's a paid API, so **by default heavy turns also stay free**: coding → the local
code model (free), everything else → free Groq-70B (strong, does tool-calling).

Set ROUTER_ALLOW_PAID=1 (and a working ANTHROPIC_API_KEY) to let heavy turns use
Claude. Until then this guarantees $0: nothing here ever selects the paid tier.
Pattern-based, no extra LLM call in the hot path.
"""

from __future__ import annotations

import os
import re
from typing import Any

from friday.core.router import Tier, get_budget

# A URL almost always means a real web task → wants the strongest reasoning available.
_URL = re.compile(r"https?://|\bwww\.", re.I)

# Strong signals a turn is hard/agentic (word-boundary matched so "plan" doesn't fire
# inside "explanation"). Broad question words are excluded to avoid over-escalating.
_HEAVY = re.compile(
    r"\b("
    r"browse|web ?site|web ?task|navigate|go to|open (?:the )?(?:site|page|url|tab)|"
    r"log ?in|sign ?in|fill (?:in|out)|submit|book|order|purchase|checkout|automate|scrape|"
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


def _allow_paid() -> bool:
    """Whether heavy turns may use the paid Claude tier (default off → always free)."""
    return os.getenv("ROUTER_ALLOW_PAID", "0").strip().lower() not in ("", "0", "false", "no")


def route(text: str, history: list[dict[str, Any]] | None = None) -> tuple[Tier, str]:
    """(tier, one-line reason). The reason is for the UI/feed, not the model."""
    t = (text or "").strip()

    paid = _allow_paid()
    if paid:  # over budget → behave as if free-only, never overspend
        try:
            b = get_budget()
            if b.daily_pct >= 100 or b.monthly_pct >= 100:
                paid = False
        except Exception:  # noqa: BLE001 — budget unavailable; fall through
            pass

    coding = bool(_CODE.search(t))
    heavy = bool(
        _URL.search(t) or _HEAVY.search(t) or coding
        or len(t.split()) > 40 or len(t) > 240
    )

    if not heavy:
        return Tier.MEDIUM, "simple — free tier"

    why = (
        "web task" if _URL.search(t)
        else "coding" if coding
        else "complex / multi-step"
    )
    if paid:
        return Tier.HEAVY, f"{why} → Claude"
    # Free heavy routing: coding to the local code model, everything else to free Groq-70B.
    if coding:
        return Tier.CODE, f"{why} → free local code model"
    return Tier.MEDIUM, f"{why} → free 70B"


def pick_tier(text: str, history: list[dict[str, Any]] | None = None) -> Tier:
    """Just the tier for a turn (free by default; Claude only if ROUTER_ALLOW_PAID)."""
    return route(text, history)[0]
