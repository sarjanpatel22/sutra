"""Dreaming — agents leave short notes about what worked / failed on a task, and recall
the relevant ones on similar future tasks.

Sutra's lightweight take on Claude Code's "Dreaming" (June 2026): a file-backed *lessons*
log, scored by the same local nomic embedder the RAG and memory-recall layers already use —
so it is $0, needs no extra infra, and **degrades to most-recent on any embed failure**, so
it can only help a run, never block it. Wired into the autonomous loop: each engagement
records a lesson, and the next similar engagement gets those lessons injected up front.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

_LESSONS = Path.home() / ".local" / "share" / "friday" / "lessons.jsonl"
_log = logging.getLogger(__name__)


def record_lesson(topic: str, lesson: str, tags: tuple[str, ...] = ()) -> None:
    """Append a lesson — what a task was + what was learned — to the dreaming log."""
    topic, lesson = (topic or "").strip(), (lesson or "").strip()
    if not lesson:
        return
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "topic": topic[:200],
        "lesson": lesson[:1000],
        "tags": list(tags),
    }
    try:
        _LESSONS.parent.mkdir(parents=True, exist_ok=True)
        with open(_LESSONS, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:  # noqa: BLE001 — a logging failure must never break the task
        _log.warning("could not record lesson: %s", e)


def _load() -> list[dict[str, Any]]:
    try:
        return [json.loads(s) for s in _LESSONS.read_text().splitlines() if s.strip()]
    except FileNotFoundError:
        return []
    except Exception as e:  # noqa: BLE001 — a corrupt log must not crash recall
        _log.warning("could not read lessons: %s", e)
        return []


def recall_lessons(query: str, k: int = 3) -> list[dict[str, Any]]:
    """The k most relevant past lessons for `query` (nomic cosine; most-recent fallback)."""
    rows = _load()
    if not rows or k <= 0:
        return []
    try:
        from friday.rag.store import embed

        qv = embed([query], "query")[0]
        docs = embed([f"{r.get('topic', '')} {r.get('lesson', '')}" for r in rows], "document")
        order = np.argsort(-(docs @ qv))[:k]
        return [rows[int(i)] for i in order]
    except Exception:  # noqa: BLE001 — Ollama down / embed error → fall back to most recent
        return rows[-k:]


def lessons_block(query: str, k: int = 3) -> str:
    """A prompt-injectable block of the relevant past lessons, or '' if there are none."""
    rows = recall_lessons(query, k)
    if not rows:
        return ""
    items = "\n".join(f"- ({r.get('topic', '?')}) {r.get('lesson', '')}" for r in rows)
    return (
        "\n\nLessons from past engagements — apply what worked, avoid repeating mistakes:\n"
        f"{items}"
    )
