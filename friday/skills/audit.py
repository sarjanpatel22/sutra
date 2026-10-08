"""Confirmation-gate audit — lock the "no irreversible action runs unconfirmed" rule.

The agent declines any auto_confirm=False skill unless a confirm hook approves it
(friday/core/agent.py `_run_tool_calls`), and that enforcement is unit-tested
(tests/test_agent.py). The remaining risk to the v1 exit criterion "action
confirmation false-negative rate = 0%" is *classification*: a genuinely irreversible
skill shipped as auto_confirm=True would execute unconfirmed. This module turns that
into a checkable invariant, shared by scripts/audit_confirmation.py (live, incl.
connected MCP tools) and tests/test_confirmation_audit.py (first-party, in CI).
"""

from __future__ import annotations

import re

from friday.skills.registry import SkillMeta

# Skill-name tokens implying a state-mutating / irreversible action. A skill whose
# name carries one MUST be gated (auto_confirm=False) unless it is an explicitly
# reviewed low-stakes/reversible exception below.
MUTATION_VERBS: frozenset[str] = frozenset({
    "write", "delete", "remove", "rm", "run", "exec", "execute", "shell", "kill",
    "send", "post", "put", "deploy", "push", "install", "uninstall", "overwrite",
    "drop", "purge", "rename", "move", "mv", "forget", "wipe", "destroy", "format",
    "revoke", "set", "open", "create", "update", "modify", "edit", "clear", "reset",
    # web/desktop action surface (Phase 1.5): anything that fills, clicks, or types
    # acts on the world and must be gated.
    "fill", "submit", "click", "type", "press", "key", "drag",
    # IDE bridge (Phase 1.6): committing writes history.
    "commit",
})

# Mutation-named skills reviewed safe to auto-run: the action is low-stakes and
# trivially reversible. Each carries its reason. Anything else matching a verb must
# be gated — that asymmetry is the false-negative tripwire.
REVIEWED_AUTO_EXCEPTIONS: dict[str, str] = {
    "clipboard_set": "overwrites clipboard text only — trivially reversible",
    "set_preference": "writes a reversible local config value, no external effect",
    "open_url": "opens a scheme-validated http(s) URL — reversible (close the tab)",
    "browser_open": "navigates Sutra's browser to an http(s) URL — reversible, sends no data",
    "open_app": "launches a desktop GUI app by name — low-stakes, reversible (close it)",
    "update_application": "reversible local tracker status/notes update, no external effect",
}

# Must exist and must be gated: a regression lock on the known irreversible surface
# (filesystem write, shell exec, destructive memory delete, and the desktop take-over
# that clicks/types on the live screen). act_on_screen carries no mutation verb in its
# name, so without this entry nothing would catch it being flipped to auto_confirm=True.
CANONICAL_GATED: frozenset[str] = frozenset(
    {"write_file", "run_command", "forget", "act_on_screen", "type_into"}
)

_SPLIT = re.compile(r"[^a-z0-9]+")


def mutation_hits(name: str) -> set[str]:
    """Mutation-verb tokens in a skill name (empty if the name reads read-only)."""
    return {t for t in _SPLIT.split(name.lower()) if t} & MUTATION_VERBS


def find_violations(metas: list[SkillMeta]) -> list[str]:
    """Auto-confirmed skills whose name implies an irreversible action — each a
    confirmation false-negative waiting to happen. Empty list == invariant holds."""
    out: list[str] = []
    for m in metas:
        if not m.auto_confirm:
            continue  # gated — safe regardless of name
        hits = mutation_hits(m.name)
        if hits and m.name not in REVIEWED_AUTO_EXCEPTIONS:
            out.append(
                f"{m.name}: auto-confirmed but name implies mutation "
                f"({', '.join(sorted(hits))}) — set auto_confirm=False or add a "
                f"reviewed exception with a reason"
            )
    return out


def canonical_problems(metas: list[SkillMeta]) -> list[str]:
    """Canonical irreversible skills that are missing or (worse) not gated."""
    by_name = {m.name: m for m in metas}
    out: list[str] = []
    for name in sorted(CANONICAL_GATED):
        m = by_name.get(name)
        if m is None:
            out.append(f"{name}: canonical irreversible skill is not registered")
        elif m.auto_confirm:
            out.append(f"{name}: canonical irreversible skill is NOT gated")
    return out


def stale_exceptions(metas: list[SkillMeta]) -> list[str]:
    """Exceptions whose skill is gone or already gated — keeps the allowlist honest."""
    by_name = {m.name: m for m in metas}
    out: list[str] = []
    for name in REVIEWED_AUTO_EXCEPTIONS:
        m = by_name.get(name)
        if m is None:
            out.append(f"{name}: reviewed exception but no such skill registered")
        elif not m.auto_confirm:
            out.append(f"{name}: reviewed exception but skill is already gated")
    return out


def all_problems(metas: list[SkillMeta]) -> list[str]:
    """Every invariant problem (violations + canonical + stale). Empty == pass."""
    return find_violations(metas) + canonical_problems(metas) + stale_exceptions(metas)


def report(metas: list[SkillMeta]) -> str:
    """Human-readable posture: each skill tagged AUTO/GATE, exceptions annotated."""
    lines = ["Confirmation-gate posture (GATE = confirmation required):", ""]
    for m in sorted(metas, key=lambda x: x.name):
        if not m.auto_confirm:
            note = "  ← irreversible, confirmation required"
        elif m.name in REVIEWED_AUTO_EXCEPTIONS:
            note = f"  (reviewed exception: {REVIEWED_AUTO_EXCEPTIONS[m.name]})"
        else:
            note = ""
        lines.append(f"  {'AUTO' if m.auto_confirm else 'GATE'}  {m.name}{note}")
    return "\n".join(lines)
