"""Skill-level tracking — a living model of what Sarjan already KNOWS, per pentest topic, so the
tutor can adapt its depth: terse hints where he's strong, real teaching where he's new.

Built up from tutoring turns (agent classifies each exchange and calls `record`) and persisted to
~/.local/share/friday/skill_profile.json, then injected into agent.tutor via `summarize()`. Distinct
from Dreaming (append-only LESSONS of what worked) — this is a mutable per-topic proficiency map.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_PROFILE = Path.home() / ".local" / "share" / "friday" / "skill_profile.json"

# Proficiency rungs, weakest → strongest — the tutor reads these to pitch its explanation depth.
LEVELS = ("new", "learning", "comfortable", "strong")
_LEVEL_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}


def _load() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_PROFILE.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(data: dict[str, Any]) -> None:
    _PROFILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PROFILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(_PROFILE)  # atomic — never leave a half-written profile


def _norm_topic(topic: str) -> str:
    return " ".join((topic or "").lower().split())[:60]


def record(topic: str, level: str, note: str = "") -> None:
    """Observe his level on a topic. Level RATCHETS UP within a topic: a momentary stumble must not
    erase demonstrated mastery (that would make the tutor condescend), but new strength is recorded.
    A real reset is `clear`. `seen` counts observations; `note` keeps the latest reason.
    """
    topic = _norm_topic(topic)
    if not topic:
        return
    level = level if level in _LEVEL_RANK else "learning"
    data = _load()
    entry = data.get(topic, {"level": "new", "seen": 0, "note": "", "updated": 0.0})
    if _LEVEL_RANK[level] >= _LEVEL_RANK.get(entry.get("level", "new"), 0):
        entry["level"] = level
    entry["seen"] = int(entry.get("seen", 0)) + 1
    if note:
        entry["note"] = note[:200]
    entry["updated"] = time.time()
    data[topic] = entry
    _save(data)


def summarize(limit: int = 12) -> str:
    """A short block of what he knows, most-recent first, for injection into the tutor prompt.
    Empty string when nothing is tracked yet (so the tutor is unchanged on a fresh install)."""
    data = _load()
    if not data:
        return ""
    items = sorted(data.items(), key=lambda kv: kv[1].get("updated", 0.0), reverse=True)[:limit]
    lines = [
        f"- {topic}: {e.get('level', 'learning')}" + (f" ({e['note']})" if e.get("note") else "")
        for topic, e in items
    ]
    return (
        "What Sarjan already knows (adapt your depth to this — be terse where he's strong, teach "
        "fully where he's new):\n" + "\n".join(lines)
    )


def clear() -> None:
    """Wipe the profile (deliberate reset)."""
    _save({})
