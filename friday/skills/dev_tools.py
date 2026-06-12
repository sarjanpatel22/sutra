"""Dev-loop skills: run the test suite from a conversation.

Phase 1.6 IDE bridge — Sutra can edit code (edit_file), see the repo (git_*),
and now prove a change with pytest. Running tests executes repo code (conftest,
fixtures, the code under test), which is arbitrary code execution by another
name, so run_tests is auto_confirm=False exactly like run_python/run_command.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from friday.skills.file_ops import _safe_path
from friday.skills.registry import skill

_MAX_OUT = 8000


@skill(
    name="run_tests",
    description=(
        "Run pytest on a path (file, directory, or whole suite) inside a repo and "
        "return the pass/fail summary plus output tail. IRREVERSIBLE (executes repo "
        "code) — requires confirmation."
    ),
    auto_confirm=False,
    timeout_s=180,
)
async def run_tests(
    path: str = "tests",
    expr: str = "",
    repo: str = "~/friday-os",
    timeout_s: int = 120,
) -> dict[str, Any]:
    """Run pytest in the agent's own interpreter against a repo.

    path: Test file or directory, relative to the repo (default "tests").
    expr: Optional pytest -k expression to select tests.
    repo: Repository root to run inside (default ~/friday-os).
    timeout_s: Kill the run after this many seconds (10-600, default 120).
    """
    try:
        root = _safe_path(repo)
    except PermissionError as e:
        return {"error": str(e)}
    if not root.is_dir():
        return {"error": f"not a directory: {root}"}
    timeout_s = max(10, min(600, timeout_s))

    argv = [sys.executable, "-m", "pytest", path, "-q", "--no-header", "-p", "no:cacheprovider"]
    if expr:
        argv += ["-k", expr]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"error": f"tests timed out after {timeout_s}s", "killed": True}
    except Exception as e:  # noqa: BLE001 — interpreter/spawn failure
        return {"error": f"could not run pytest: {e}"}

    out = out_b.decode(errors="replace")
    tail = [ln for ln in out.splitlines() if ln.strip()]
    summary = tail[-1] if tail else ""
    return {
        "exit_code": proc.returncode,
        "passed": proc.returncode == 0,
        "summary": summary,
        "output": out[-_MAX_OUT:],
    }
