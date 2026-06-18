"""Tests for `see_screen` — fully mocked: no real screenshot, no network, read-only."""

from __future__ import annotations

import pytest

import friday.skills.see_screen as see
from friday.skills import registry
from friday.skills.see_screen import see_screen
from friday.vision.screen import ScreenUnavailable


def _reply(text: str):
    """Fake litellm.acompletion matching .choices[0].message.content access."""

    async def fake(model, messages, **k):
        msg = type("M", (), {"content": text})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})

    return fake


def _boom(exc: Exception):
    async def fake(model, messages, **k):
        raise exc

    return fake


def _fake_grab(log: list[str] | None = None):
    async def fake(*a, **k) -> bytes:
        if log is not None:
            log.append("grab")
        return b"fakejpeg"

    return fake


async def test_describes_the_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(see, "screen_available", lambda: True)
    monkeypatch.setattr(see, "grab_jpeg", _fake_grab())
    monkeypatch.setattr(
        see.litellm, "acompletion", _reply("A terminal with an nmap scan running.")
    )
    out = await see_screen()
    assert out == {"description": "A terminal with an nmap scan running."}


async def test_answers_a_question_about_the_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(see, "screen_available", lambda: True)
    monkeypatch.setattr(see, "grab_jpeg", _fake_grab())
    monkeypatch.setattr(see.litellm, "acompletion", _reply("  Yes, the scan finished.  "))
    out = await see_screen("is the scan done?")
    assert out == {"description": "Yes, the scan finished."}


async def test_no_screen_returns_error_and_no_vision(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    monkeypatch.setattr(see, "screen_available", lambda: False)
    monkeypatch.setattr(see, "grab_jpeg", _fake_grab(log))
    monkeypatch.setattr(see.litellm, "acompletion", _boom(AssertionError("must not call")))
    out = await see_screen()
    assert "error" in out
    assert "screen" in out["error"].lower()
    assert log == []  # short-circuited before capture


async def test_screen_unavailable_on_grab(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(see, "screen_available", lambda: True)

    async def boom(*a, **k) -> bytes:
        raise ScreenUnavailable("display vanished")

    monkeypatch.setattr(see, "grab_jpeg", boom)
    monkeypatch.setattr(see.litellm, "acompletion", _boom(AssertionError("must not call")))
    out = await see_screen()
    assert "error" in out
    assert "screen" in out["error"].lower()


async def test_vision_error_is_caught_and_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(see, "screen_available", lambda: True)
    monkeypatch.setattr(see, "grab_jpeg", _fake_grab())
    monkeypatch.setattr(see.litellm, "acompletion", _boom(RuntimeError("x" * 500)))
    out = await see_screen("what's open?")
    assert "error" in out
    assert "description" not in out  # no success payload on failure
    assert len(out["error"]) < 220


def test_skill_is_read_only_auto_confirm() -> None:
    registry.autodiscover()
    assert registry.get("see_screen").auto_confirm is True


def test_skill_autodiscovers() -> None:
    registry.autodiscover()
    names = [t["function"]["name"] for t in registry.all_tools()]
    assert "see_screen" in names
