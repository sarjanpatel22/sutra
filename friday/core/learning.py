"""sutra learning — a window into what Sutra is learning and has learned.

Read-only aggregator over the learning surfaces, powering `sutra learning [--watch]`:
  * the study loop's digests — what each pass concluded ([[project_sutra_killchain_wired]])
  * the playbook doctrine — operating principles it has earned
  * Dreaming lessons — what worked on real engagements
  * the ingested knowledge base — RAG collections it can recall from

Pure data (a snapshot); the CLI renders it. Everything degrades to empty on any failure, so the
monitor can only ever under-report, never crash.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class LearningSnapshot:
    timer_active: bool = False
    last_study: str = "never"
    doctrine: list[str] = field(default_factory=list)          # learned principles, newest first
    digests: list[tuple[str, str]] = field(default_factory=list)   # (date, headline) recent passes
    lessons: list[tuple[str, str]] = field(default_factory=list)   # (topic, lesson) recent
    collections: list[tuple[str, int]] = field(default_factory=list)  # (name, chunk_count)


def _timer_active() -> bool:
    """True if the 24/7 study timer is armed (systemctl --user). False if systemd/timer absent."""
    if not shutil.which("systemctl"):
        return False
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "sutra-study.timer"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def _digest_headline(path: Path) -> str:
    """The gist of a study digest — the '## Learned' bullets (proposed principles) or the no-data
    note — so the monitor shows what each pass concluded at a glance."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    out: list[str] = []
    grabbing = False
    for ln in lines:
        if ln.startswith("## Learned"):
            grabbing = True
            continue
        if grabbing:
            if ln.startswith("## "):
                break
            s = ln.strip().lstrip("-").strip()
            if s:
                out.append(s)
    return " · ".join(out[:2]) or "(nothing new)"


def _list_collections() -> list[tuple[str, int]]:
    """Ingested RAG knowledge collections and their chunk counts."""
    out: list[tuple[str, int]] = []
    try:
        from friday.rag.store import _RAG_DIR, RagStore
    except Exception:  # noqa: BLE001 — RAG optional / import failure → no knowledge shown
        return out
    try:
        dirs = sorted(d for d in _RAG_DIR.iterdir() if d.is_dir())
    except OSError:
        return out
    for d in dirs:
        try:
            out.append((d.name, len(RagStore(d.name))))
        except Exception:  # noqa: BLE001 — a broken collection shouldn't hide the others
            out.append((d.name, 0))
    return out


def snapshot() -> LearningSnapshot:
    """Gather the current learning state across every surface. Never raises."""
    from friday.core import dreaming, playbook, study

    digest_paths = study.latest_digests(5)
    last = "never"
    if digest_paths:
        try:
            last = datetime.fromtimestamp(
                digest_paths[0].stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            pass
    digests = [(p.stem, _digest_headline(p)) for p in digest_paths]

    doctrine = list(reversed(playbook._load_learned()))[:8]

    lessons_raw = dreaming._load()[-6:]
    lessons = [(str(r.get("topic", "?"))[:40], str(r.get("lesson", ""))[:140])
               for r in reversed(lessons_raw) if str(r.get("lesson", "")).strip()]

    return LearningSnapshot(
        timer_active=_timer_active(), last_study=last, doctrine=doctrine,
        digests=digests, lessons=lessons, collections=_list_collections(),
    )


def snapshot_dict() -> dict[str, Any]:
    """The snapshot as a JSON-serialisable dict — what the web dashboard polls."""
    s = snapshot()
    return {
        "timer_active": s.timer_active,
        "last_study": s.last_study,
        "doctrine": s.doctrine,
        "digests": [{"date": d, "headline": h} for d, h in s.digests],
        "lessons": [{"topic": t, "lesson": ln} for t, ln in s.lessons],
        "collections": [{"name": n, "chunks": c} for n, c in s.collections],
    }
