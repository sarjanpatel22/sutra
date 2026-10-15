"""sutra study — Sutra's daily self-improvement pass: reflect on REAL run data, distill new
grounded doctrine, and leave a dated digest to review.

This is the scheduler that drives the compounding brain ([[playbook]] + [[train]] + [[dreaming]]).
Run it on a timer (or at boot). Each pass:

  1. GATHERS real signals only — engagement traces, dreaming lessons, the current playbook size,
     and engagement readiness. No invented material.
  2. REFLECTS with the FREE model (free_only — never spends the paid budget, takes NO actions) to
     pull at most a couple of NEW, grounded operating principles out of what actually happened,
     plus skill-hygiene notes and a suggested next focus.
  3. RECORDS the new principles into the playbook (deduped, tagged source="study", capped) so the
     doctrine grows from experience, and writes a dated digest to ~/.local/share/friday/study/.

Honest by design: with no real data yet it does NOT hallucinate lessons — it skips the model call,
writes a short digest saying so, and points at the next step (run live boxes). $0; read-only
against the world (it only writes its own digest + grows the local playbook log).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from friday.core.router import Tier, complete

_STUDY_DIR = Path.home() / ".local" / "share" / "friday" / "study"
# A weak daily reflector must not flood the doctrine; dedup in record_principle also applies.
_MAX_NEW_PRINCIPLES = 2
_log = logging.getLogger(__name__)


@dataclass
class Signals:
    n_traces: int = 0
    n_successful: int = 0
    recent_outcomes: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    playbook_size: int = 0
    readiness: str = ""

    @property
    def has_material(self) -> bool:
        """Only reflect when there is real experience to learn from — otherwise we'd fabricate."""
        return bool(self.n_traces or self.lessons)


@dataclass
class Reflection:
    principles: list[str] = field(default_factory=list)
    hygiene: list[str] = field(default_factory=list)
    focus: str = ""
    raw: str = ""


def gather_signals(max_recent: int = 8) -> Signals:
    """Collect the real, grounded evidence of how Sutra has been doing — nothing invented."""
    from friday.core import agent, dreaming, playbook, train

    traces = train.load_traces()
    successful = 0
    outcomes: list[str] = []
    for tr in traces:
        msgs, report = tr.get("messages") or [], tr.get("report", "")
        flags, findings = agent.engagement_highlights(msgs, report)
        if flags or findings:
            successful += 1
    for tr in traces[-max_recent:]:
        rep = " ".join((tr.get("report") or "").split())
        outcomes.append(f"[{(tr.get('goal') or '?')[:50]}] {rep[:160]}")

    lessons = [str(r.get("lesson", "")).strip() for r in dreaming._load()[-max_recent:]]
    lessons = [ln for ln in lessons if ln]

    try:
        from friday.core.doctor import engagement_rows
        rows = engagement_rows()
        ok = sum(1 for r in rows if r.status.value.lower() == "ok")
        readiness = f"{ok}/{len(rows)} engagement-readiness checks OK"
    except Exception:  # noqa: BLE001 — readiness is a nicety, never block the study pass
        readiness = "readiness unavailable"

    return Signals(
        n_traces=len(traces), n_successful=successful, recent_outcomes=outcomes,
        lessons=lessons, playbook_size=len(playbook.principles(limit=0)), readiness=readiness,
    )


_REFLECT_SYS = (
    "You are Sutra reflecting on your OWN recent authorized security-engagement work to get better "
    "at it. You are given real evidence: past engagement outcomes and lessons. Reason ONLY from "
    "that evidence. Each principle must be specific to what actually happened — NOT generic "
    "textbook security knowledge, and not a fact about a specific exploit. If the evidence does "
    "not clearly support a new principle, return an empty list; an empty list is the right "
    "answer when there is nothing genuinely new to learn. Reply with ONLY a JSON object:\n"
    '{"principles": ["a new operating principle clearly earned from THIS evidence", ...], '
    '"hygiene": ["a note on a skill/tool that under- or mis-performed in the runs", ...], '
    '"focus": "the single most valuable thing to try next"}'
)


def _signals_to_prompt(s: Signals) -> str:
    lines = [
        f"Engagements run: {s.n_traces} (reached a flag or finding: {s.n_successful}).",
        f"Current playbook size: {s.playbook_size} principles. Readiness: {s.readiness}.",
    ]
    if s.recent_outcomes:
        lines += ["", "Recent engagement outcomes:", *(f"- {o}" for o in s.recent_outcomes)]
    if s.lessons:
        lines += ["", "Recent lessons:", *(f"- {ln}" for ln in s.lessons)]
    lines += ["", "What did you learn? Return the JSON object."]
    return "\n".join(lines)


def _parse_reflection(text: str) -> dict[str, Any]:
    """Tolerantly pull the JSON object out of the model's reply; {} if it isn't parseable (we then
    record nothing rather than fabricate)."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


async def reflect(signals: Signals, *, tier: Tier = Tier.MEDIUM) -> Reflection:
    """One grounded reflection over the signals, at the FREE tier (never paid, no tools/actions)."""
    messages = [
        {"role": "system", "content": _REFLECT_SYS},
        {"role": "user", "content": _signals_to_prompt(signals)},
    ]
    resp = await complete(messages, tier=tier, tools=None, free_only=True)
    text = (resp.choices[0].message.content or "").strip()
    data = _parse_reflection(text)

    def _strs(key: str) -> list[str]:
        return [str(x).strip() for x in (data.get(key) or []) if str(x).strip()]

    return Reflection(principles=_strs("principles")[:_MAX_NEW_PRINCIPLES],
                      hygiene=_strs("hygiene"), focus=str(data.get("focus") or "").strip(),
                      raw=text)


def _write_digest(signals: Signals, refl: Reflection | None, *, wrote: bool) -> Path:
    day = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# Sutra study — {day}", "",
             "## Reviewed",
             f"- engagements: {signals.n_traces} ({signals.n_successful} reached a flag/finding)",
             f"- lessons on file: {len(signals.lessons)}",
             f"- playbook: {signals.playbook_size} principles",
             f"- readiness: {signals.readiness}", ""]
    if refl is None:
        lines += ["## Learned", "",
                  "No engagement data yet — nothing to learn from honestly. Run some live boxes "
                  "(`sutra pwn <ip> -y`) and this loop starts compounding.", ""]
    else:
        verb = "added to the playbook" if wrote else "proposed — not applied (re-run with --apply)"
        lines += [f"## Learned — new principles ({verb})", ""]
        lines += [f"- {p}" for p in refl.principles] or ["- (none this pass)"]
        lines += ["", "## Skill hygiene", ""] + ([f"- {h}" for h in refl.hygiene] or ["- (none)"])
        lines += ["", "## Suggested next focus", "", refl.focus or "- (none)", ""]
    _STUDY_DIR.mkdir(parents=True, exist_ok=True)
    path = _STUDY_DIR / f"{day}.md"
    try:
        path.write_text("\n".join(lines))
    except OSError as e:
        _log.warning("could not write study digest %s: %s", path, e)
    return path


def _state_key(signals: Signals) -> str:
    """A cheap fingerprint of the learnable material. It changes only when there is something new
    to reflect on, so a frequent (24/7) timer can SKIP the model call — and the free-tier rate
    limit — whenever nothing has changed since the last pass."""
    return f"{signals.n_traces}:{len(signals.lessons)}"


def _load_marker() -> str:
    try:
        return (_STUDY_DIR / ".last").read_text().strip()
    except OSError:
        return ""


def _save_marker(key: str) -> None:
    try:
        _STUDY_DIR.mkdir(parents=True, exist_ok=True)
        (_STUDY_DIR / ".last").write_text(key)
    except OSError as e:
        _log.warning("could not write study marker: %s", e)


async def run_study(*, write: bool = True, tier: Tier = Tier.MEDIUM,
                    force: bool = False) -> tuple[Path, Reflection | None, str]:
    """One self-improvement pass, safe to run continuously. Gathers real signals; if there is
    nothing NEW to learn since the last pass it SKIPS the model call (so a 24/7 timer stays cheap
    and never thrashes the free-tier rate limit) unless force=True. Otherwise it reflects, may grow
    the playbook, and writes the dated digest. Returns (path, reflection_or_None, status) where
    status is 'reflected' | 'no-data' | 'unchanged'. $0, takes no actions."""
    signals = gather_signals()
    if not signals.has_material:
        return _write_digest(signals, None, wrote=write), None, "no-data"
    key = _state_key(signals)
    if not force and key == _load_marker():
        digests = latest_digests(1)
        return (digests[0] if digests else _STUDY_DIR), None, "unchanged"
    refl = await reflect(signals, tier=tier)
    if write and refl.principles:
        from friday.core import playbook
        for p in refl.principles:
            playbook.record_principle(p, source="study")  # deduped against core + learned
    path = _write_digest(signals, refl, wrote=write)
    _save_marker(key)  # remember the state we just reflected on
    return path, refl, "reflected"


def latest_digests(n: int = 3) -> list[Path]:
    """The most recent study digests, newest first — what to review each session."""
    try:
        return sorted(_STUDY_DIR.glob("*.md"), reverse=True)[:n]
    except OSError:
        return []
