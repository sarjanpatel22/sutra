"""Tests for run_python — real isolated subprocesses (fast), covering the gated-exec contract."""

from __future__ import annotations

from friday.skills import registry
from friday.skills.code_exec import run_python


def test_registered_and_gated():
    registry.autodiscover()
    meta = registry.get("run_python")
    # arbitrary code execution is irreversible → must NOT be auto-confirmed
    assert meta.auto_confirm is False


async def test_empty_code_is_error():
    out = await run_python("   ")
    assert "error" in out and "no code" in out["error"]


async def test_runs_and_captures_stdout():
    out = await run_python("print(2 + 3)")
    assert out["exit_code"] == 0
    assert out["stdout"].strip() == "5"
    assert out["stderr"] == ""


async def test_nonzero_exit_and_stderr_on_exception():
    out = await run_python("raise ValueError('boom')")
    assert out["exit_code"] != 0
    assert "ValueError" in out["stderr"] and "boom" in out["stderr"]


async def test_timeout_kills_runaway():
    out = await run_python("while True: pass", timeout_s=1)
    assert out.get("killed") is True and "timed out" in out["error"]


async def test_timeout_is_clamped():
    # timeout_s is clamped to 1..60; a huge value must not be honored literally.
    # A trivial program still returns promptly (proves the clamp didn't block forever).
    out = await run_python("print('ok')", timeout_s=9999)
    assert out["exit_code"] == 0 and out["stdout"].strip() == "ok"


async def test_isolated_mode_ignores_user_site():
    # `python -I` runs isolated: sys.flags.isolated is 1 and the cwd isn't on sys.path,
    # so a planted module in the working dir can't hijack an import.
    out = await run_python("import sys; print(sys.flags.isolated)")
    assert out["stdout"].strip() == "1"


async def test_stdout_is_truncated():
    out = await run_python("print('x' * 20000)")
    assert len(out["stdout"]) <= 8000
