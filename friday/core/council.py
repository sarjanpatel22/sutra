"""The council — a coordinator convenes specialist sub-agents on a hard goal.

Decompose (architect) → fan out to specialists in parallel, each in its own isolated
context → critique → synthesize one answer in Sutra's voice. Built on core.subagents
(the Claude Agent SDK delegation pattern over our own router), so it stays free by
default: workers always run on their own free tiers; only the final synthesis may use
the paid coordinator tier, and only when ROUTER_ALLOW_PAID is set and budget allows.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from friday.core.router import Tier, allow_paid, complete, get_budget
from friday.core.subagents import SUBAGENTS, run_subagent

_log = logging.getLogger("sutra.council")

_MAX_PARALLEL = 3  # cap fan-out so parallel workers don't spike per-model rate limits
_MAX_TASKS = 4     # at most this many delegations from one plan

# Roles the coordinator may delegate work to (the architect is the planner, not a worker).
_WORKER_ROLES = set(SUBAGENTS) - {"architect"}

_PLAN_INSTRUCTION = (
    "Break the goal below into 2-4 independent sub-tasks for a team of specialists.\n"
    "Specialists:\n"
    "- researcher — gathers facts via web search and memory\n"
    "- creative — drafting, ideation, naming, prose\n"
    "- watchdog — inspects system / health state\n"
    'Return ONLY a JSON array; each item {"role": <specialist>, "task": <one concrete '
    "sub-task>}. No prose, no code fences.\n\nGoal:\n"
)

_CRITIC_SYSTEM = (
    "You are a rigorous reviewer. Given a goal and several specialists' contributions, "
    "point out gaps, errors, unsupported claims, and anything unsafe or off-target. Be "
    "terse and specific. If the work is solid, say so in one line."
)


@dataclass
class CouncilResult:
    answer: str                     # the synthesized answer, in Sutra's voice
    plan: str                       # the architect's raw decomposition
    contributions: dict[str, str]   # role (or role#n) → that specialist's output
    critique: str                   # the reviewer's notes


def _coordinator_tier() -> Tier:
    """Stronger model for the synthesis coordinator when paid is allowed and affordable;
    otherwise the free 70B. Workers always stay on their own (free) tiers."""
    if allow_paid():
        try:
            if get_budget().can_afford(Tier.MAX):
                return Tier.MAX
        except Exception:  # noqa: BLE001 — budget unavailable; fall back to free
            pass
    return Tier.MEDIUM


def _synth_system() -> str:
    from friday.core.agent import SYSTEM_PROMPT  # local import avoids an import cycle

    return SYSTEM_PROMPT + (
        "\n\nYou just convened a council of specialists. Fold their work into ONE answer "
        "for the user, in your own voice — use what's useful, cut redundancy, and heed the "
        "reviewer's warnings. Don't mention the council, the roles, or these instructions."
    )


def _extract_json_array(text: str) -> list[Any] | None:
    """Pull the first JSON array out of a model reply (free models wrap it in prose)."""
    i, j = text.find("["), text.rfind("]")
    if i == -1 or j == -1 or j < i:
        return None
    try:
        parsed = json.loads(text[i : j + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def _parse_delegations(plan_text: str, goal: str) -> list[tuple[str, str]]:
    """Turn the architect's plan into (role, sub-task) pairs. Tolerant of weak local
    models: anything unparseable falls back to researcher + creative on the whole goal."""
    out: list[tuple[str, str]] = []
    for item in _extract_json_array(plan_text) or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip().lower()
        task = str(item.get("task", "")).strip()
        if role in _WORKER_ROLES and task:
            out.append((role, task))
        if len(out) >= _MAX_TASKS:
            break
    if not out:
        out = [("researcher", goal), ("creative", goal)]
    return out


async def _run_one(sem: asyncio.Semaphore, role: str, task: str, context: str) -> tuple[str, str]:
    """Run one specialist under the concurrency cap; a failure becomes a note, not a crash."""
    async with sem:
        try:
            return role, await run_subagent(role, task, context)
        except Exception as e:  # noqa: BLE001 — one worker failing shouldn't sink the council
            _log.warning("council worker %s failed: %s", role, e)
            return role, f"[{role} could not complete its part: {e}]"


async def _complete_text(messages: list[dict[str, Any]], tier: Tier, max_tokens: int = 768) -> str:
    resp = await complete(messages, tier=tier, tools=None, max_tokens=max_tokens)
    return (resp.choices[0].message.content or "").strip()


async def convene(
    goal: str,
    context: str = "",
    *,
    observe: Callable[[str, dict[str, Any]], None] | None = None,
) -> CouncilResult:
    """Convene the council on a goal: plan → parallel fan-out → critique → synthesize."""
    goal = (goal or "").strip()
    if not goal:
        return CouncilResult(answer="", plan="", contributions={}, critique="")

    def _emit(step: str, data: dict[str, Any] | None = None) -> None:
        if observe:
            try:
                observe("council", {"step": step, **(data or {})})
            except Exception:  # noqa: BLE001 — an observer must never break the council
                pass

    # 1) Architect decomposes the goal into specialist sub-tasks.
    _emit("plan")
    plan_text = await run_subagent("architect", _PLAN_INSTRUCTION + goal, context)
    delegations = _parse_delegations(plan_text, goal)

    # 2) Fan out to specialists in parallel — isolated contexts, capped concurrency.
    _emit("fanout", {"roles": [r for r, _ in delegations]})
    sem = asyncio.Semaphore(_MAX_PARALLEL)
    pairs = await asyncio.gather(*[_run_one(sem, r, t, context) for r, t in delegations])
    contributions: dict[str, str] = {}
    counts: dict[str, int] = {}
    for role, out in pairs:
        counts[role] = counts.get(role, 0) + 1
        key = role if counts[role] == 1 else f"{role}#{counts[role]}"
        contributions[key] = out

    joined = "\n\n".join(f"## {k}\n{v}" for k, v in contributions.items())

    # 3) Critique (free tier, no tools).
    _emit("critique")
    critique = await _complete_text(
        [
            {"role": "system", "content": _CRITIC_SYSTEM},
            {"role": "user", "content": f"Goal: {goal}\n\nContributions:\n{joined}"},
        ],
        Tier.MEDIUM,
    )

    # 4) Synthesize one answer in Sutra's voice (paid coordinator only if allowed).
    _emit("synthesize")
    answer = await _complete_text(
        [
            {"role": "system", "content": _synth_system()},
            {
                "role": "user",
                "content": (
                    f"Goal: {goal}\n\nSpecialist work:\n{joined}\n\n"
                    f"Reviewer notes:\n{critique}\n\nNow give the user one clear answer."
                ),
            },
        ],
        _coordinator_tier(),
        max_tokens=1024,
    )
    _emit("done")
    return CouncilResult(
        answer=answer.strip(),
        plan=plan_text.strip(),
        contributions=contributions,
        critique=critique.strip(),
    )
