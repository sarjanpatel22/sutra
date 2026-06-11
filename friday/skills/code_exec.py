"""Gated code execution: run a Python snippet in an isolated subprocess.

The Open-Interpreter slot of Phase 1.5, done as the pattern rather than the heavy
open-interpreter package (which defaults to paid OpenAI and drives its own loop): the
agent's LLM already writes the code, this just runs it. Each run is a fresh `python
-I` subprocess (isolated mode — ignores PYTHONPATH / user-site / cwd injection) with
a hard timeout, so a runaway or crash can't touch the agent process.

Arbitrary code is as powerful as a shell, so run_python is auto_confirm=False — the
user confirms each run and a sub-agent declines it, exactly like run_command. The
subprocess + timeout is isolation, not a security sandbox; the confirm gate is the
real control (same honest posture as run_command).
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from friday.skills.registry import skill

_MAX_OUT = 8000  # truncate captured stdout/stderr to keep model context cheap


@skill(
    name="run_python",
    description=(
        "Execute a snippet of Python code in an isolated subprocess and return its "
        "stdout, stderr, and exit code. Use for real computation, data wrangling, or "
        "quick scripts. IRREVERSIBLE (arbitrary code execution) — requires confirmation."
    ),
    auto_confirm=False,
    timeout_s=40,
)
async def run_python(code: str, timeout_s: int = 15) -> dict[str, Any]:
    """Run Python code in a fresh, isolated subprocess.

    code: The Python source to execute.
    timeout_s: Kill the subprocess after this many seconds (1-60, default 15).
    """
    code = code.strip()
    if not code:
        return {"error": "no code to run"}
    timeout_s = max(1, min(60, timeout_s))
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-",  # -I: isolated (no PYTHONPATH/user-site/cwd), read stdin
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:  # noqa: BLE001 — interpreter missing / spawn failure
        return {"error": f"could not start python: {e}"}

    try:
        out, err = await asyncio.wait_for(proc.communicate(code.encode()), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"error": f"timed out after {timeout_s}s", "killed": True}

    return {
        "exit_code": proc.returncode,
        "stdout": out.decode(errors="replace")[:_MAX_OUT],
        "stderr": err.decode(errors="replace")[:_MAX_OUT],
    }
