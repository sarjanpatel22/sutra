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
    r = runner.invoke(cli.app, ["auto", "pwn it", "--tier", "medium", "-y"])
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
    r = runner.invoke(cli.app, ["auto", "scan it", "--tier", "medium", "-y"])
    assert r.exit_code == 0, r.output
    assert "Recon only" in r.output
    assert "Flags captured" not in r.output  # nothing captured → no flags panel
