"""Tests for gated desktop-input skills (xdotool).

Real input injection is NOT fired at the live session (it would type into / click on
whatever window has focus). Instead we lock: the input skills are gated, arg-validation
rejects bad input before xdotool runs, and the exact xdotool argv is built correctly
(via a mocked runner). active_window (read-only) is the only one safe to run live.
"""

from friday.skills import desktop_input as di
from friday.skills import registry


def _fake_runner(calls: list[list[str]]):
    async def _run(cmd, stdin=None):
        calls.append(cmd)
        return 0, "", ""

    return _run


def test_input_skills_are_gated_and_query_is_auto():
    registry.autodiscover()
    for name in ("type_text", "press_key", "mouse_click"):
        assert registry.get(name).auto_confirm is False, name
    assert registry.get("active_window").auto_confirm is True  # read-only query


async def test_type_text_rejects_empty():
    assert "no text" in (await di.type_text(""))["error"]


async def test_press_key_rejects_empty():
    assert "no keys" in (await di.press_key("   "))["error"]


async def test_mouse_click_rejects_bad_button():
    assert "button must be" in (await di.mouse_click(button=9))["error"]


async def test_type_text_builds_correct_argv(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(di, "_run", _fake_runner(calls))
    monkeypatch.setattr(di.shutil, "which", lambda _: "/usr/bin/xdotool")
    out = await di.type_text("hello world")
    assert out == {"status": "ok", "typed_chars": 11}
    assert calls == [["xdotool", "type", "--clearmodifiers", "--", "hello world"]]


async def test_press_key_builds_sequence_argv(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(di, "_run", _fake_runner(calls))
    monkeypatch.setattr(di.shutil, "which", lambda _: "/usr/bin/xdotool")
    await di.press_key("ctrl+s Return")
    assert calls == [["xdotool", "key", "--clearmodifiers", "ctrl+s", "Return"]]


async def test_mouse_click_moves_then_clicks(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(di, "_run", _fake_runner(calls))
    monkeypatch.setattr(di.shutil, "which", lambda _: "/usr/bin/xdotool")
    await di.mouse_click(button=3, x=100, y=200)
    assert calls == [
        ["xdotool", "mousemove", "100", "200"],
        ["xdotool", "click", "3"],
    ]


async def test_xdotool_missing_returns_clean_error(monkeypatch):
    monkeypatch.setattr(di.shutil, "which", lambda _: None)
    assert "not installed" in (await di.type_text("x"))["error"]
