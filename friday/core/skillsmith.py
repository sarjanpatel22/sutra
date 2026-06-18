"""Self-extending skills — Sutra DRAFTS a skill from a description; you REVIEW it; you APPROVE
it; only then does it load.

Safety is the whole point. Drafting writes inert *source* to a pending dir — it never execs,
imports, or registers the code. A static scan flags risky patterns for your review. Approval
(`sutra skill approve`, your explicit action) moves the file into the user-skills dir, where it
loads on the NEXT run — so nothing Sutra writes ever runs without your sign-off. "Anything —
with your permission."
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

from friday.core.router import Tier, complete
from friday.skills.registry import USER_SKILLS_DIR

_PENDING_DIR = Path.home() / ".local" / "share" / "friday" / "pending_skills"
_log = logging.getLogger(__name__)

DRAFT_SYSTEM_PROMPT = (
    "You write ONE self-contained Python module defining a single Sutra skill. Output ONLY the "
    "code — no prose, no markdown fences. Follow this template exactly:\n\n"
    "from friday.skills.registry import skill\n\n"
    '@skill(name="<snake_case>", description="<what it does, for the model>", '
    "auto_confirm=<True only if READ-ONLY and reversible, else False>, timeout_s=<int>)\n"
    "async def <snake_case>(<typed args with defaults>) -> dict:\n"
    '    """<one line + arg notes>"""\n'
    "    ...\n"
    "    return {<json-able results>}\n\n"
    "Rules: prefer the standard library; type-hint every argument; return a JSON-able dict; set "
    "auto_confirm=False for ANYTHING that writes, deletes, sends, or runs commands. Do NOT use "
    "subprocess, os.system, eval, or exec; do NOT delete files. Only touch the network if the "
    "skill's whole purpose is an API call — then use httpx and auto_confirm=False. Keep it small "
    "and focused."
)

# Patterns worth a human's eyes before approval. Not a blocker — the review IS the gate — but
# every flag is surfaced so you never approve something risky without seeing it.
_RISKY: list[tuple[str, str]] = [
    (r"\bimport\s+subprocess\b|\bsubprocess\.", "spawns processes (subprocess)"),
    (r"\bos\.system\b|\bos\.popen\b", "runs a shell command (os.system/popen)"),
    (r"\beval\s*\(", "evaluates dynamic code (eval)"),
    (r"\bexec\s*\(", "executes dynamic code (exec)"),
    (r"__import__", "dynamic import (__import__)"),
    (r"\bshutil\.rmtree\b", "recursively deletes a directory (shutil.rmtree)"),
    (r"\bos\.(remove|unlink|rmdir)\b", "deletes files (os.remove/unlink/rmdir)"),
    (r"\b(socket|requests|urllib|httpx)\b", "makes network calls"),
    (r"\bopen\s*\([^)]*['\"][wax]", "writes to a file (open in write/append mode)"),
    (r"\bpickle\b", "deserializes pickle (code-execution risk)"),
    (r"\bctypes\b", "calls native code (ctypes)"),
]


def scan_code(code: str) -> list[str]:
    """Human-readable flags for risky patterns in `code` (empty == nothing flagged)."""
    return [msg for pat, msg in _RISKY if re.search(pat, code)]


def _clean(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
        raw = re.sub(r"\n```\s*$", "", raw)
    return raw.strip()


def _extract_name(code: str, fallback: str) -> str:
    m = re.search(r"""@skill\([^)]*name\s*=\s*["']([a-zA-Z0-9_]+)["']""", code)
    if m:
        return m.group(1)
    m = re.search(r"async\s+def\s+([a-zA-Z0-9_]+)", code)
    return m.group(1) if m else fallback


async def draft_skill(description: str, *, tier: Tier = Tier.MEDIUM) -> dict:
    """Draft a skill from `description`. Writes inert source to the pending dir — never runs it.

    Returns {name, path, code, flags} on success, or {error, ...}. The code is validated as
    parseable Python (ast.parse, which does NOT execute it) and scanned, but only YOU decide
    whether it becomes real, via `approve_skill`.
    """
    description = description.strip()
    if not description:
        return {"error": "describe what the skill should do"}
    messages = [
        {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
        {"role": "user", "content": description},
    ]
    resp = await complete(messages, tier=tier, tools=None, max_tokens=900)
    code = _clean(resp.choices[0].message.content or "")
    if not code:
        return {"error": "the model returned no code"}
    try:
        ast.parse(code)  # validate syntax only — this does NOT execute the code
    except SyntaxError as e:
        return {"error": f"the draft did not parse as Python: {e}", "code": code}
    slug = re.sub(r"[^a-z0-9]+", "_", description.lower()).strip("_")[:30] or "skill"
    name = _extract_name(code, slug)
    _PENDING_DIR.mkdir(parents=True, exist_ok=True)
    path = _PENDING_DIR / f"{name}.py"
    path.write_text(code)
    return {"name": name, "path": str(path), "code": code, "flags": scan_code(code)}


def list_pending() -> list[str]:
    """Names of skills drafted and awaiting your review/approval."""
    return sorted(p.stem for p in _PENDING_DIR.glob("*.py")) if _PENDING_DIR.is_dir() else []


def read_pending(name: str) -> str | None:
    p = _PENDING_DIR / f"{name}.py"
    return p.read_text() if p.is_file() else None


def approve_skill(name: str) -> Path | None:
    """Promote a pending skill into the user-skills dir (loads next run). Your explicit go."""
    src = _PENDING_DIR / f"{name}.py"
    if not src.is_file():
        return None
    USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    dst = USER_SKILLS_DIR / f"{name}.py"
    dst.write_text(src.read_text())
    src.unlink()
    return dst


def reject_skill(name: str) -> bool:
    """Discard a pending draft."""
    p = _PENDING_DIR / f"{name}.py"
    if p.is_file():
        p.unlink()
        return True
    return False
