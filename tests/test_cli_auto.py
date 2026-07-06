"""Tests for the `sutra auto` / `pwn` CLI surfacing flags + findings live.

The loop's engagement file already leads with the flags; these prove the CLI shows them on
screen the moment the run ends — even when the model's prose report forgot to restate a flag it
merely cat'd — by running the real engagement_highlights over a canned (report, messages).
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

import friday.cli as cli
from friday.core import agent

runner = CliRunner()


class _FakeStdin:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_detached_reflects_stdin_tty(monkeypatch):
    """_detached() is True only with no interactive terminal attached — the guard that makes a
    backgrounded auto/pwn/plan decline risky steps loudly (hinting --yes) instead of hanging on a
    dead y/n prompt (the bug where a detached `pwn` silently stalled at recon)."""
    monkeypatch.setattr(cli.sys, "stdin", _FakeStdin(tty=False))
    assert cli._detached() is True
    monkeypatch.setattr(cli.sys, "stdin", _FakeStdin(tty=True))
    assert cli._detached() is False


def test_auto_surfaces_flags_and_findings_live(monkeypatch, tmp_path):
    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)

    async def fake_run_autonomous(goal, **kw):
        msgs = [
            {"role": "tool", "name": "run_command",
             "content": json.dumps({"command": "cat user.txt", "stdout": "THM{live_flag}\n"})},
            {"role": "tool", "name": "port_scan",
             "content": json.dumps({"host": "10.10.10.9", "open": [{"port": 80, "banner": ""}]})},
        ]
        return "Compromised the box.", msgs  # prose report does NOT restate the flag

    monkeypatch.setattr(agent, "run_autonomous", fake_run_autonomous)
    r = runner.invoke(cli.app, ["auto", "pwn it", "--tier", "medium", "-y", "--single"])
    assert r.exit_code == 0, r.output
    assert "THM{live_flag}" in r.output      # the flag surfaces though the report omitted it
    assert "Flags captured" in r.output
    assert "10.10.10.9" in r.output          # findings recap too


def test_auto_no_flags_still_prints_report(monkeypatch, tmp_path):
    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)

    async def fake_run_autonomous(goal, **kw):
        return "Recon only — no foothold yet.", [{"role": "assistant", "content": "looked around"}]

    monkeypatch.setattr(agent, "run_autonomous", fake_run_autonomous)
    r = runner.invoke(cli.app, ["auto", "scan it", "--tier", "medium", "-y", "--single"])
    assert r.exit_code == 0, r.output
    assert "Recon only" in r.output
    assert "Flags captured" not in r.output  # nothing captured → no flags panel


def test_auto_tiered_routes_to_orchestrator_and_streams_live(monkeypatch, tmp_path):
    from friday.core.router import Tier

    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)
    used = {}

    async def fake_tiered(goal, *, orchestrator_tier=None, confirm=None, observe=None,
                          scope=None, **kw):
        used["tier"] = orchestrator_tier
        if observe:  # the orchestrator's objective, a worker tool call, the worker's digest
            observe("objective", {"text": "port_scan 10.10.10.9"})
            observe("tool_start", {"skill": "port_scan", "args": {"host": "10.10.10.9"}})
            observe("worker_report", {"text": "22/SSH, 80/Apache"})
        msgs = [{"role": "tool", "name": "run_command",
                 "content": json.dumps({"stdout": "THM{tier_cli}"})}]
        return "Rooted the box.", msgs

    async def boom(*a, **k):
        raise AssertionError("run_autonomous must not run in tiered mode")

    monkeypatch.setattr(agent, "run_engagement_tiered", fake_tiered)
    monkeypatch.setattr(agent, "run_autonomous", boom)

    r = runner.invoke(cli.app, ["auto", "pwn it", "--tier", "heavy", "-y"])  # tiered is default now
    assert r.exit_code == 0, r.output
    assert used["tier"] is Tier.HEAVY                       # --tier heavy → orchestrator tier
    assert "objective" in r.output and "port_scan 10.10.10.9" in r.output  # orchestrator streamed
    assert "worker:" in r.output and "22/SSH" in r.output   # worker digest streamed
    assert "THM{tier_cli}" in r.output and "Flags captured" in r.output    # flag surfaced live


def test_pwn_tiered_threads_objective_and_round_caps(monkeypatch, tmp_path):
    # The bounded-probe knobs (--objectives / --worker-rounds) reach the engine, so a first live
    # run can be cheap (e.g. --objectives 2 = at most 2 paid orchestrator calls).
    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)
    seen = {}

    async def fake_tiered(goal, *, orchestrator_tier=None, max_objectives=8, worker_rounds=6,
                          confirm=None, observe=None, scope=None, **kw):
        seen.update(objectives=max_objectives, worker_rounds=worker_rounds, scope=scope)
        return "done", []

    monkeypatch.setattr(agent, "run_engagement_tiered", fake_tiered)
    r = runner.invoke(cli.app, ["pwn", "10.10.10.5", "-y",  # tiered is default now
                                "--objectives", "2", "--worker-rounds", "3"])
    assert r.exit_code == 0, r.output
    assert seen["objectives"] == 2 and seen["worker_rounds"] == 3
    assert seen["scope"] == "10.10.10.5"  # pwn still auto-scopes to its target
