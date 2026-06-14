"""CLI tests for the desktop commands — `guide`, `do`, and `watch --once`.

Everything that would touch the real machine is monkeypatched: `guide_me`,
`act_on_screen`, and `watch_once` are stubbed so NOTHING is captured, NOTHING is
clicked, and no vision call leaves the box. We drive the Typer app through
`CliRunner` and assert on stdout + on whether the irreversible action ran.
"""

from __future__ import annotations

from typer.testing import CliRunner

import friday.cli as cli

runner = CliRunner()


# ── guide (read-only) ────────────────────────────────────────────────────────────

def test_guide_prints_guidance(monkeypatch) -> None:
    async def fake_guide(goal: str) -> dict:
        return {"goal": goal, "guidance": "Click the green Deploy button, top-right."}

    monkeypatch.setattr("friday.skills.guide.guide_me", fake_guide)
    result = runner.invoke(cli.app, ["guide", "deploy the app"])
    assert result.exit_code == 0
    assert "Deploy button" in result.stdout


def test_guide_prints_error(monkeypatch) -> None:
    async def fake_guide(_goal: str) -> dict:
        return {"error": "I can't see your screen right now."}

    monkeypatch.setattr("friday.skills.guide.guide_me", fake_guide)
    result = runner.invoke(cli.app, ["guide", "whatever"])
    assert result.exit_code == 0
    assert "can't see your screen" in result.stdout


# ── do (irreversible — confirmation gated) ───────────────────────────────────────

def _spy_act(calls: list[str]):
    async def fake_act(goal: str) -> dict:
        calls.append(goal)
        return {"acted": True, "goal": goal, "target": "Save", "coords": [12, 34],
                "note": "Clicked 'Save'."}

    return fake_act


def test_do_declined_does_not_click(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("friday.skills.desktop_control.act_on_screen", _spy_act(calls))
    # Answer "n" to the confirmation prompt.
    result = runner.invoke(cli.app, ["do", "press Save"], input="n\n")
    assert result.exit_code == 0
    assert "Cancelled." in result.stdout
    assert calls == []  # the destructive skill must NOT have run


def test_do_confirmed_clicks(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("friday.skills.desktop_control.act_on_screen", _spy_act(calls))
    result = runner.invoke(cli.app, ["do", "press Save"], input="y\n")
    assert result.exit_code == 0
    assert calls == ["press Save"]
    assert "Clicked" in result.stdout
    assert "Save" in result.stdout


def test_do_yes_flag_skips_prompt(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("friday.skills.desktop_control.act_on_screen", _spy_act(calls))
    result = runner.invoke(cli.app, ["do", "-y", "press Save"])
    assert result.exit_code == 0
    assert calls == ["press Save"]
    assert "Clicked" in result.stdout


def test_do_yes_honest_no_match(monkeypatch) -> None:
    async def fake_act(goal: str) -> dict:
        return {"acted": False, "goal": goal, "note": "Nothing clearly matched 'foo'."}

    monkeypatch.setattr("friday.skills.desktop_control.act_on_screen", fake_act)
    result = runner.invoke(cli.app, ["do", "-y", "foo"])
    assert result.exit_code == 0
    assert "Nothing clearly matched" in result.stdout


# ── watch --once ─────────────────────────────────────────────────────────────────

def test_watch_once_prints_suggestion(monkeypatch) -> None:
    async def fake_once(*_a, **_k) -> str | None:
        return "Pin that dependency version."

    monkeypatch.setattr("friday.watch.loop.watch_once", fake_once)
    result = runner.invoke(cli.app, ["watch", "--once"])
    assert result.exit_code == 0
    assert "Pin that dependency version." in result.stdout


def test_watch_once_prints_quiet_line(monkeypatch) -> None:
    async def fake_once(*_a, **_k) -> str | None:
        return None

    monkeypatch.setattr("friday.watch.loop.watch_once", fake_once)
    result = runner.invoke(cli.app, ["watch", "--once"])
    assert result.exit_code == 0
    assert "Nothing worth interrupting you for" in result.stdout
