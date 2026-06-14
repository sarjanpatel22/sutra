"""Tests for `guide_me` — fully mocked: no real screenshot, no network, read-only."""

from __future__ import annotations

import pytest

import friday.skills.guide as guide
from friday.skills import registry
from friday.skills.guide import guide_me
from friday.vision.screen import ScreenUnavailable


def _reply(text: str):
    """Build a fake litellm.acompletion matching .choices[0].message.content access."""

    async def fake(model, messages, **k):
        msg = type("M", (), {"content": text})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})

    return fake


def _boom(exc: Exception):
    async def fake(model, messages, **k):
        raise exc

    return fake


def _fake_grab(log: list[str] | None = None):
    """An async grab_jpeg stand-in returning fixed bytes, optionally logging the call."""

    async def fake(*a, **k) -> bytes:
        if log is not None:
            log.append("grab")
        return b"fakejpeg"

    return fake


async def test_guides_to_the_button(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guide, "screen_available", lambda: True)
    monkeypatch.setattr(guide, "grab_jpeg", _fake_grab())
    monkeypatch.setattr(
        guide.litellm, "acompletion", _reply("Click the blue Save button, bottom-right.")
    )
    out = await guide_me("save my work")
    assert out == {
        "goal": "save my work",
        "guidance": "Click the blue Save button, bottom-right.",
    }


async def test_strips_goal_and_model_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guide, "screen_available", lambda: True)
    monkeypatch.setattr(guide, "grab_jpeg", _fake_grab())
    monkeypatch.setattr(guide.litellm, "acompletion", _reply("  Press the gear icon.  "))
    out = await guide_me("  open settings  ")
    assert out == {"goal": "open settings", "guidance": "Press the gear icon."}


async def test_empty_goal_skips_capture_and_vision(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    monkeypatch.setattr(guide, "screen_available", lambda: True)
    monkeypatch.setattr(guide, "grab_jpeg", _fake_grab(log))
    monkeypatch.setattr(guide.litellm, "acompletion", _boom(AssertionError("must not call")))
    out = await guide_me("   ")
    assert "error" in out
    assert log == []  # never grabbed the screen


async def test_no_screen_returns_error_and_no_vision(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    monkeypatch.setattr(guide, "screen_available", lambda: False)
    monkeypatch.setattr(guide, "grab_jpeg", _fake_grab(log))
    monkeypatch.setattr(guide.litellm, "acompletion", _boom(AssertionError("must not call")))
    out = await guide_me("deploy the app")
    assert "error" in out
    assert "screen" in out["error"].lower()
    assert log == []  # short-circuited before capture


async def test_screen_unavailable_on_grab(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guide, "screen_available", lambda: True)

    async def boom(*a, **k) -> bytes:
        raise ScreenUnavailable("display vanished")

    monkeypatch.setattr(guide, "grab_jpeg", boom)
    monkeypatch.setattr(guide.litellm, "acompletion", _boom(AssertionError("must not call")))
    out = await guide_me("open settings")
    assert "error" in out
    assert "screen" in out["error"].lower()


async def test_vision_error_is_caught_and_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guide, "screen_available", lambda: True)
    monkeypatch.setattr(guide, "grab_jpeg", _fake_grab())
    monkeypatch.setattr(guide.litellm, "acompletion", _boom(RuntimeError("x" * 500)))
    out = await guide_me("find the export button")
    assert "error" in out
    assert "goal" not in out  # no success payload on failure
    assert len(out["error"]) < 220  # message truncated, did not crash


def test_skill_is_read_only_auto_confirm() -> None:
    registry.autodiscover()
    assert registry.get("guide_me").auto_confirm is True


def test_skill_autodiscovers() -> None:
    registry.autodiscover()
    names = [t["function"]["name"] for t in registry.all_tools()]
    assert "guide_me" in names
