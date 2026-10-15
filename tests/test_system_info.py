"""Tests for system_info / battery / run_command's non-safety paths.

The catastrophic-command floor (is_catastrophic + run_command refusal) is covered
thoroughly in test_autonomous.py; this file covers the remaining behavior.
"""

from __future__ import annotations

from friday.skills import system_info as si
from friday.skills.system_info import battery, run_command, system_info


async def test_system_info_returns_core_keys():
    out = await system_info()
    for key in ("platform", "cpu_percent", "ram_total_gb", "disk_total_gb", "uptime_hours"):
        assert key in out
    assert isinstance(out["ram_total_gb"], float) and out["ram_total_gb"] > 0


async def test_battery_handles_no_battery(monkeypatch):
    monkeypatch.setattr(si.psutil, "sensors_battery", lambda: None)
    out = await battery()
    assert "error" in out and "no battery" in out["error"]


async def test_battery_reports_when_present(monkeypatch):
    class _Bat:
        percent = 83.4
        power_plugged = True
        secsleft = 3600

    monkeypatch.setattr(si.psutil, "sensors_battery", lambda: _Bat())
    out = await battery()
    assert out["percent"] == 83.4 and out["plugged_in"] is True
    assert out["minutes_left"] == 60


async def test_run_command_executes_and_captures():
    out = await run_command("printf 'hello'")
    assert out["returncode"] == 0 and out["stdout"] == "hello"


async def test_run_command_nonzero_returncode():
    out = await run_command("sh -c 'echo oops >&2; exit 3'")
    assert out["returncode"] == 3 and "oops" in out["stderr"]
