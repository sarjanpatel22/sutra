"""Automatic tier selection — Sutra picks free-medium vs Claude-heavy herself.

The router has tiers; this decides which one a turn deserves so the user never has to.
The split mirrors how Sarjan framed it: simple turns (a fact, a greeting, one quick
tool call) ride the free MEDIUM tier (Groq/Gemini, $0); genuinely hard turns —
multi-step web tasks, coding, real analysis, anything long — go HEAVY (Claude), where
tool-use and planning are reliable (that's where the free models were erroring out).
Pattern-based, no extra LLM call in the hot path.

HEAVY costs money, so it's bounded two ways: if the budget is spent we stay on the
free tier, and even when we route HEAVY the router falls back to MEDIUM's free models
when there's no Claude key/credit (intra-tier fallback).
"""

from __future__ import annotations

import re
from typing import Any

from friday.core.router import Tier, get_budget

# A URL almost always means a real web task → Claude-grade planning.
_URL = re.compile(r"https?://|\bwww\.", re.I)

# Strong signals a turn needs Claude reliability (word-boundary matched so "plan"
# doesn't fire inside "explanation"). Kept to genuinely hard/agentic markers — broad
# question words ("why is", "how do i") are deliberately excluded to avoid paying
# Claude for simple questions.
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

# Coding intent → Claude (reliable multi-step / tool reasoning).
_CODE = re.compile(
    r"\b(write (?:me )?(?:a |some )?(?:code|script|function|program)|unit test|pytest|"
    r"git (?:commit|diff|status|push)|pull request|refactor|stack ?trace|"
    r"fix (?:the|this|my) (?:bug|code|function|error))\b",
    re.I,
)


def route(text: str, history: list[dict[str, Any]] | None = None) -> tuple[Tier, str]:
    """(tier, one-line reason). The reason is for the UI/feed, not the model."""
    t = (text or "").strip()
    # Never route to paid Claude once the budget is spent — stay free.
    try:
        b = get_budget()
        if b.daily_pct >= 100 or b.monthly_pct >= 100:
            return Tier.MEDIUM, "budget reached — staying on the free tier"
    except Exception:  # noqa: BLE001 — budget unavailable; fall through to routing
        pass

    if _URL.search(t):
        return Tier.HEAVY, "has a URL — real web task"
    if _HEAVY.search(t):
        return Tier.HEAVY, "multi-step / analytical / agentic"
    if _CODE.search(t):
        return Tier.HEAVY, "coding task"
    if len(t.split()) > 40 or len(t) > 240:
        return Tier.HEAVY, "long / complex request"
    return Tier.MEDIUM, "simple turn — free tier"


def pick_tier(text: str, history: list[dict[str, Any]] | None = None) -> Tier:
    """Just the tier: MEDIUM for simple turns (free), HEAVY for hard ones (Claude)."""
    return route(text, history)[0]
