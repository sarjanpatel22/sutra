"""Task planner — decompose a big goal into small steps, run them one at a time, and
PERSIST progress so a long task survives token/budget exhaustion and resumes exactly where
it stopped.

Why this exists: a weak model loses the thread on a long multi-step goal, and a strong one
burns tokens trying to do it all in one breath — so Sutra "can't last 3 minutes" and "can't
divide the work." The planner has the strong model PLAN once (understand + divide — the hard
part), then executes each small step with whatever tier fits, saving after every step. So a
big goal becomes a checklist that works across sessions and resumes after a rate-limit/budget
pause.

This module (PR1) is just the durable data model: a `Plan` of ordered `Step`s, saved to disk
as JSON, loadable by slug. Decomposition (`make_plan`) and execution (`run_plan`) build on it.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from friday.core.router import Tier, complete

# Hooks threaded into step execution (same shapes the agent uses).
ConfirmFn = Callable[[str, dict], Awaitable[bool]]   # (skill, args) -> allow?
ObserveFn = Callable[[str, dict], None]              # (kind, data) live progress

_PLANS_DIR = Path.home() / ".local" / "share" / "friday" / "plans"

# Step lifecycle. PENDING → DONE (ran ok) / FAILED (ran, errored) / SKIPPED (deliberately).
PENDING = "pending"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"
_DONEISH = (DONE, SKIPPED)  # counts as "no longer needs doing"


@dataclass
class Step:
    """One small, concrete unit of work — small enough that any tier can run it reliably."""

    n: int
    description: str
    status: str = PENDING
    result: str = ""  # short note / outcome, filled in once it runs


@dataclass
class Plan:
    """An ordered checklist for a goal, with per-step status persisted to disk."""

    goal: str
    slug: str
    steps: list[Step] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    # active = still has pending steps; done = all finished; paused = stopped on budget;
    # failed = gave up. The loop owns transitions; persistence just records them.
    status: str = "active"

    def next_pending(self) -> Step | None:
        """The first step still to do, or None when the plan is finished."""
        return next((s for s in self.steps if s.status == PENDING), None)

    def is_complete(self) -> bool:
        """True once every step is done/skipped (and there is at least one step)."""
        return bool(self.steps) and all(s.status in _DONEISH for s in self.steps)

    def progress(self) -> tuple[int, int]:
        """(finished, total) for a one-line status."""
        return sum(s.status in _DONEISH for s in self.steps), len(self.steps)


def slugify(goal: str) -> str:
    """Stable, filesystem-safe id for a goal — also the handle for `resume <slug>`."""
    return re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")[:40] or "plan"


def _plan_path(slug: str) -> Path:
    return _PLANS_DIR / f"{slug}.json"


def new_plan(goal: str, step_descriptions: list[str]) -> Plan:
    """Build a Plan from a goal + ordered step descriptions. Does NOT save — the caller
    decides when to persist (so a freshly-decomposed plan can be reviewed first)."""
    # renumber AFTER dropping blanks, so step ids stay sequential (1,2,3…)
    cleaned = [d.strip() for d in step_descriptions if d and d.strip()]
    steps = [Step(n=i + 1, description=d) for i, d in enumerate(cleaned)]
    return Plan(goal=goal.strip(), slug=slugify(goal), steps=steps)


def save(plan: Plan) -> Path:
    """Persist the plan to disk atomically, so progress survives a crash / token reset."""
    _PLANS_DIR.mkdir(parents=True, exist_ok=True)
    path = _plan_path(plan.slug)
    data = {
        "goal": plan.goal,
        "slug": plan.slug,
        "status": plan.status,
        "created_at": plan.created_at,
        "steps": [
            {"n": s.n, "description": s.description, "status": s.status, "result": s.result}
            for s in plan.steps
        ],
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)  # atomic rename — never leaves a half-written plan on disk
    return path


def load(slug: str) -> Plan | None:
    """Load a saved plan by slug, or None if it's missing or corrupt (tolerant by design —
    a damaged plan file must never crash a resume)."""
    path = _plan_path(slug)
    if not path.is_file():
        return None
    try:
        d = json.loads(path.read_text())
        steps = [
            Step(
                n=int(s["n"]),
                description=s["description"],
                status=s.get("status", PENDING),
                result=s.get("result", ""),
            )
            for s in d.get("steps", [])
        ]
        return Plan(
            goal=d["goal"],
            slug=d["slug"],
            steps=steps,
            created_at=float(d.get("created_at", 0.0)),
            status=d.get("status", "active"),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def list_plans() -> list[Plan]:
    """All saved plans, newest first — for `sutra plan list`."""
    if not _PLANS_DIR.is_dir():
        return []
    loaded = [p for f in _PLANS_DIR.glob("*.json") if (p := load(f.stem))]
    return sorted(loaded, key=lambda p: p.created_at, reverse=True)


# --- decomposition: the strong model divides the goal into small steps -------------

_PLAN_SYSTEM = (
    "You are a planner. Break the user's goal into a SHORT ordered list of small, concrete "
    "steps that a tool-using assistant executes one at a time. Each step is ONE action — a "
    "single web search, one file write, one command, one check. Use the FEWEST steps that "
    "actually accomplish the goal (usually 3-8; never more than 12). Do not include vague "
    "steps like 'analyze' or 'think'. Output ONLY a JSON array of step strings — no prose, no "
    'markdown fences. Example: ["search the web for X", "write a summary to ~/out.md"]'
)


def _parse_steps(raw: str) -> list[str]:
    """Pull an ordered step list out of the model's reply. Prefers a JSON array (what the
    prompt asks for); falls back to numbered / bulleted lines so a chatty model still works."""
    s = (raw or "").strip()
    # Reasoning models (qwen3, etc.) wrap chain-of-thought in <think>…</think>. Strip it, or
    # every THOUGHT line becomes a fake step (caught live: an 8-"step" plan that was just the
    # model thinking). Handle a complete block, content after a close tag, and a dangling
    # open block (the model ran out of tokens mid-thought → no real answer).
    s = re.sub(r"(?is)<think>.*?</think>", "", s)
    if "</think>" in s:
        s = s.rsplit("</think>", 1)[-1]
    s = re.sub(r"(?is)<think>.*$", "", s).strip()
    s = re.sub(r"^```[a-zA-Z]*\n?", "", s)  # strip a leading ```json fence
    s = re.sub(r"\n?```\s*$", "", s).strip()
    try:
        data = json.loads(s)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except json.JSONDecodeError:
        pass
    # Fallback: one step per non-empty line, stripping "1." / "2)" / "- " / "* " markers.
    out: list[str] = []
    for line in s.splitlines():
        line = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", line.strip())
        if line and not line.startswith(("{", "[", "}")):
            out.append(line)
    return out


async def make_plan(goal: str, *, tier: Tier = Tier.HEAVY, max_steps: int = 12) -> Plan:
    """Decompose `goal` into an ordered Plan using the strong model — this is the
    'understand + divide the work' part, so it goes cloud-first (Claude when paid, the free
    70B otherwise), never the small local model. Does NOT save; the caller persists it."""
    goal = (goal or "").strip()
    if not goal:
        return Plan(goal="", slug="plan", steps=[], status="failed")
    messages = [
        {"role": "system", "content": _PLAN_SYSTEM},
        {"role": "user", "content": goal},
    ]
    # Generous max_tokens: a reasoning model (qwen3) spends a big chunk THINKING before it
    # emits the JSON, so 800 cut it off mid-thought with no plan. 2500 lets it finish.
    resp = await complete(
        messages, tier=tier, tools=None, local_tools_first=False, max_tokens=2500
    )
    steps = _parse_steps(resp.choices[0].message.content or "")[:max_steps]
    plan = new_plan(goal, steps)
    if not plan.steps:
        plan.status = "failed"  # the model returned nothing usable — surfaced, not silent
    return plan


# --- execution: run steps one at a time, persist after each, pause to resume later --

def _budget_ok() -> bool:
    """True if there's budget to keep going. Only PAID work can run out — on the free tiers
    there's nothing to exhaust — so this gates the Claude path and lets a free plan run on."""
    from friday.core.router import _budget, allow_paid

    if not allow_paid():
        return True
    return _budget.can_afford(Tier.HEAVY)


def _step_prompt(plan: Plan, step: Step) -> str:
    """Ground one step in the overall goal + what's already done, so the executor stays on
    track and doesn't redo finished work or wander off the objective."""
    done = [s for s in plan.steps if s.status == DONE]
    context = ""
    if done:
        context = "Already completed:\n" + "\n".join(f"- {s.description}" for s in done) + "\n\n"
    return (
        f"You are working through a larger goal: {plan.goal}\n\n"
        f"{context}Do ONLY this next step, then stop:\n{step.description}"
    )


async def run_plan(
    plan: Plan,
    *,
    confirm: ConfirmFn | None = None,
    observe: ObserveFn | None = None,
) -> Plan:
    """Execute the plan's PENDING steps one at a time, saving after each so progress survives
    a crash or token reset. Pauses — leaving the current step PENDING — when the budget is
    exhausted OR a step errors (e.g. a rate limit), so a later `resume_plan` picks up at
    exactly that step instead of losing the work. Returns the final plan."""
    from friday.core.agent import run as agent_run

    plan.status = "active"
    save(plan)
    while (step := plan.next_pending()) is not None:
        if not _budget_ok():
            plan.status = "paused"  # over budget — stop; step stays PENDING for resume
            save(plan)
            return plan
        if observe:
            observe("step_start", {"n": step.n, "description": step.description})
        try:
            reply, _ = await agent_run(_step_prompt(plan, step), confirm=confirm, observe=observe)
        except Exception as e:  # noqa: BLE001 — a transient failure must not lose the plan;
            step.result = f"paused after error: {e}"[:500]  # leave the step PENDING to retry
            plan.status = "paused"
            save(plan)
            if observe:
                observe("step_end", {"n": step.n, "status": "paused"})
            return plan
        step.status = DONE
        step.result = (reply or "").strip()[:500]
        save(plan)  # persist after EVERY step — the resumability guarantee
        if observe:
            observe("step_end", {"n": step.n, "status": step.status})
    plan.status = "done" if plan.is_complete() else "failed"
    save(plan)
    return plan


async def resume_plan(
    slug: str,
    *,
    confirm: ConfirmFn | None = None,
    observe: ObserveFn | None = None,
) -> Plan | None:
    """Reload a saved plan and continue from its first unfinished step — the 'after a token
    reset, pick up where you left off' entry point. None if no such plan exists."""
    plan = load(slug)
    if plan is None:
        return None
    return await run_plan(plan, confirm=confirm, observe=observe)
