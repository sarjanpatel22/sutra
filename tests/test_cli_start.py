"""`sutra start` and bare `sutra` boot the presence (orb + chat)."""

from __future__ import annotations

from typer.testing import CliRunner

import friday.cli as cli
from friday.core.router import Tier

runner = CliRunner()


def test_start_command_boots_presence_with_orb(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "_start_presence",
                        lambda tier, *, orb=True: called.update(tier=tier, orb=orb))
    r = runner.invoke(cli.app, ["start"])
    assert r.exit_code == 0, r.output
    assert called["orb"] is True and called["tier"] is Tier.MEDIUM


def test_start_no_orb_flag(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "_start_presence", lambda tier, *, orb=True: called.update(orb=orb))
    r = runner.invoke(cli.app, ["start", "--no-orb"])
    assert r.exit_code == 0, r.output
    assert called["orb"] is False


def test_bare_sutra_boots_presence(monkeypatch):
    # The headline: `sutra` with NO subcommand boots the presence instead of just printing help.
    called = {}
    monkeypatch.setattr(cli, "_start_presence", lambda tier, *, orb=True: called.update(ran=True))
    r = runner.invoke(cli.app, [])
    assert r.exit_code == 0, r.output
    assert called.get("ran") is True


def test_subcommand_does_not_boot_presence(monkeypatch):
    # A real subcommand must NOT trigger the bare-start path (only the callback's secret load runs).
    called = {}
    monkeypatch.setattr(cli, "_start_presence", lambda *a, **k: called.update(ran=True))
    runner.invoke(cli.app, ["skills"])
    assert "ran" not in called


def test_start_presence_survives_orb_failure(monkeypatch):
    # The orb is ambient — if serve_background can't start (no display), chat still runs.
    def boom(*a, **k):
        raise RuntimeError("no display")

    async def fake_chat(tier):
        return None

    monkeypatch.setattr("friday.gui.server.serve_background", boom)
    monkeypatch.setattr(cli, "_chat_loop", fake_chat)
    cli._start_presence(Tier.MEDIUM, orb=True)  # must not raise — the orb failure is swallowed
