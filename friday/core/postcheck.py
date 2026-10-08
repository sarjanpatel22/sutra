"""Verify-before-claim — the phantom-success killer.

After an acting skill runs, check its REAL postcondition so the agent can never
report a success that didn't actually happen (the #1 agentic-reliability failure:
"Sutra said it did the thing but didn't"). A verifier is

    (args, result) -> (ok: bool, note: str) | None

where `None` means "can't verify" (unknown is NOT treated as failure). Only skills
with a registered verifier are checked; everything else is left untouched. A verifier
that raises is treated as unverifiable — a verifier bug must never break the loop or
falsely fail a working skill.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

Verifier = Callable[[dict[str, Any], Any], "tuple[bool, str] | None"]
_VERIFIERS: dict[str, Verifier] = {}


def register(name: str, fn: Verifier) -> None:
    """Register a postcondition verifier for a skill name."""
    _VERIFIERS[name] = fn


def verify(name: str, args: dict[str, Any], result: Any) -> tuple[bool, str] | None:
    """(ok, note) if a verifier exists and can judge; None if unverifiable."""
    fn = _VERIFIERS.get(name)
    if fn is None:
        return None
    try:
        return fn(args, result)
    except Exception:  # noqa: BLE001 — a verifier must never break dispatch
        return None


# --- built-in verifiers for the core acting skills -------------------------
def _v_run_command(args: dict[str, Any], result: Any) -> tuple[bool, str] | None:
    """A non-zero exit code is a failure — even though run_command returns no
    'error' key, so the loop would otherwise score it 'ok' (a phantom success)."""
    if not isinstance(result, dict) or "returncode" not in result:
        return None
    rc = result.get("returncode", 0)
    if rc == 0:
        return True, "exit 0"
    stderr = str(result.get("stderr", "") or "")[:200]
    return False, f"command exited with code {rc}" + (f"; stderr: {stderr}" if stderr else "")


def _v_write_file(args: dict[str, Any], result: Any) -> tuple[bool, str] | None:
    """The file must actually exist, with the byte count that was reported."""
    if not isinstance(result, dict) or "path" not in result:
        return None
    p = Path(result["path"])
    if not p.exists():
        return False, f"file was not created at {p}"
    try:
        want = int(result.get("bytes_written", -1))
    except (TypeError, ValueError):
        want = -1
    if want >= 0 and p.stat().st_size != want:
        return False, f"file exists but is {p.stat().st_size} bytes, not the {want} reported"
    return True, f"file present at {p}"


def _v_edit_file(args: dict[str, Any], result: Any) -> tuple[bool, str] | None:
    """The file must still exist and actually contain the new text."""
    if not isinstance(result, dict) or "path" not in result:
        return None
    p = Path(result["path"])
    if not p.exists():
        return False, f"file missing at {p}"
    new_text = args.get("new_text")
    if isinstance(new_text, str) and new_text:
        try:
            if new_text not in p.read_text(errors="replace"):
                return False, "edit reported success but new_text is not in the file"
        except OSError:
            return None
    return True, f"edit present in {p}"


register("run_command", _v_run_command)
register("write_file", _v_write_file)
register("edit_file", _v_edit_file)
