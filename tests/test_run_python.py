"""Tests for run_python — gated, isolated subprocess code execution.

Unlike the browser skills, this needs no network: it really runs short Python in a
subprocess, so the happy path, error path, timeout, and isolation are all checked live.
"""

from friday.skills import registry
from friday.skills.code_exec import run_python


def test_run_python_is_gated():
    registry.autodiscover()
    assert registry.get("run_python").auto_confirm is False  # arbitrary code → confirm


async def test_run_python_rejects_empty():
    assert "no code" in (await run_python("   "))["error"]


async def test_run_python_executes_and_captures_stdout():
    out = await run_python("print(6 * 7)")
    assert out["exit_code"] == 0
    assert out["stdout"].strip() == "42"
    assert out["stderr"] == ""


async def test_run_python_reports_errors():
    out = await run_python("raise ValueError('boom')")
    assert out["exit_code"] != 0
    assert "ValueError" in out["stderr"] and "boom" in out["stderr"]


async def test_run_python_times_out_and_is_killed():
    out = await run_python("import time; time.sleep(5)", timeout_s=1)
    assert out.get("killed") is True
    assert "timed out" in out["error"]


async def test_run_python_is_isolated_from_agent_env(monkeypatch):
    # -I mode: a PYTHONPATH injected into the agent must not reach the child.
    monkeypatch.setenv("PYTHONPATH", "/tmp/should-not-appear")
    out = await run_python("import sys; print('/tmp/should-not-appear' in sys.path)")
    assert out["stdout"].strip() == "False"
