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
from dataclasses import dataclass, field
from pathlib import Path

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
