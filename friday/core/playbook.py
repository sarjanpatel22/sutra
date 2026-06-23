"""Playbook — Sutra's operating doctrine: *how* to work, distilled from how Claude works.

Dreaming ([[dreaming]]) records what was learned about a *specific* task and recalls it on
similar ones. The playbook is the layer above that: task-independent *method* — the handful of
principles that make the difference between a model that knows pentesting and an operator that
actually gets a box. Sarjan asked to "give Sutra some of your work and train it so it knows how
you work" — we can't fine-tune weights, but we can hand Sutra the working doctrine in-context and
inject it into every autonomous run, so the brain compounds.

Two layers:
  * `_CORE_PRINCIPLES` — the seed, distilled from how Claude operates. Always present.
  * a file-backed *learned* log (`record_principle`) — new principles earned from real runs,
    so the doctrine grows. Deduped against the core and each other; bounded; $0; degrades to
    just the core on any read failure, so it can only help a run, never block it.

`playbook_block()` renders both into a prompt-injectable block, wired in next to
`dreaming.lessons_block` in the autonomous and orchestrator system prompts.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from friday.core.turnlog import append_capped

_STORE = Path.home() / ".local" / "share" / "friday" / "playbook.jsonl"
# Bound the learned log — it is read (and deduped) on every autonomous run, so keep it cheap.
_MAX_LEARNED = 200
_log = logging.getLogger(__name__)

# The seed doctrine — how Claude works, distilled to the principles that change an outcome.
# Phrased as direct instruction to Sutra; each is something a strong operator does without being
# told. Kept tight so the whole set fits in a prompt without crowding out the task.
_CORE_PRINCIPLES: tuple[str, ...] = (
    "Decompose before you act: break the goal into the smallest steps you can verify, do them "
    "one at a time, and confirm each before moving on.",
    "Ground every claim in real tool output — if you did not run it, you do not know it. Never "
    "report progress you did not observe.",
    "Verify before you call it done: re-check the result against what was actually asked; a task "
    "is not finished until you have seen it work.",
    "Take the cheapest path that works first — the local, free, simple tool — and escalate to the "
    "expensive one only when the cheap one genuinely cannot do it.",
    "Anticipate the whole failure class, not just the example in front of you: when you fix or "
    "harden something, handle the entire category at once.",
    "Checkpoint progress: save state after each step so an interruption never strands the work, "
    "and always leave things in a resumable state.",
    "Act on what is reversible; confirm what is not. Move fast on safe, undoable actions; stop and "
    "ask before anything destructive or outward-facing.",
    "When stuck, change the approach — do not repeat it. Two failed tries on the same path means "
    "the path is wrong; switch technique, target, or angle.",
    "Prefer the purpose-built tool over improvising: reach for the dedicated skill before "
    "hand-rolling it with a raw command — it is more reliable and easier to read.",
    "Report honestly, including what failed: state plainly what worked, what did not, and what is "
    "still blocked. Never dress up a partial result as success.",
)


def _norm(text: str) -> str:
    """Normalize a principle for dedup — case- and whitespace-insensitive."""
    return " ".join((text or "").lower().split())


def _load_learned() -> list[str]:
    """The learned principles (strings), oldest first. Empty on missing/corrupt — never raises."""
    try:
        rows = [json.loads(s) for s in _STORE.read_text().splitlines() if s.strip()]
    except FileNotFoundError:
        return []
    except Exception as e:  # noqa: BLE001 — a corrupt log must not crash recall
        _log.warning("could not read playbook: %s", e)
        return []
    out = []
    for r in rows:
        p = str(r.get("principle", "")).strip() if isinstance(r, dict) else ""
        if p:
            out.append(p)
    return out


def principles(limit: int = 12) -> list[str]:
    """Core doctrine + learned principles, deduped (case/space-insensitive). Core always comes
    first and is never crowded out; learned principles follow most-recent-first. `limit<=0`
    returns everything (used for the dedup check)."""
    out: list[str] = []
    seen: set[str] = set()
    for p in (*_CORE_PRINCIPLES, *reversed(_load_learned())):
        key = _norm(p)
        if key and key not in seen:
            seen.add(key)
            out.append(p)
    return out if limit <= 0 else out[:limit]


def record_principle(principle: str, *, source: str = "learned") -> bool:
    """Add a new operating principle to the learned playbook. Deduped against the core doctrine
    and everything already learned. Returns True iff it was newly added (False if blank/duplicate
    or the write failed). Never raises — a logging failure must not break the task."""
    text = " ".join((principle or "").split())
    if not text:
        return False
    if _norm(text) in {_norm(p) for p in principles(limit=0)}:
        return False
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "principle": text[:300],
        "source": source,
    }
    try:
        append_capped(_STORE, json.dumps(rec), _MAX_LEARNED)
        return True
    except Exception as e:  # noqa: BLE001 — a logging failure must never break the task
        _log.warning("could not record principle: %s", e)
        return False


def playbook_block(limit: int = 12) -> str:
    """A prompt-injectable block of the operating doctrine, or '' if somehow empty."""
    items = principles(limit)
    if not items:
        return ""
    body = "\n".join(f"- {p}" for p in items)
    return "\n\nHow you operate — your working doctrine, apply it to every task:\n" + body
