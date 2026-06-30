"""Tests for `friday.skills.desktop_control` — everything mocked: no OCR, clicks, or LLM."""

from __future__ import annotations

import pytest

from friday.skills import desktop_control as dc
from friday.skills.desktop_control import (
    _core,
    _score,
    act_on_screen,
    best_match,
    type_into,
)
from friday.vision.locate import Element


def _el(text: str, x: int = 0, y: int = 0, w: int = 10, h: int = 10, conf: float = 90.0) -> Element:
    return Element(text=text, x=x, y=y, w=w, h=h, conf=conf)


def _ok_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make _locate believe the tools exist so it proceeds to OCR/match."""
    monkeypatch.setattr(dc, "ocr_available", lambda: True)
    monkeypatch.setattr(dc, "_XDOTOOL", "/usr/bin/xdotool")


def _ocr_returns(monkeypatch: pytest.MonkeyPatch, elements: list[Element]) -> None:
    async def fake_ocr(*a: object, **k: object) -> list[Element]:
        return elements

    monkeypatch.setattr(dc, "_ocr_elements", fake_ocr)  # perceive-backed OCR seam


def _click_recorder(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    calls: list[tuple[int, int]] = []

    async def fake_click(x: int, y: int, button: int = 1) -> None:
        calls.append((x, y))

    monkeypatch.setattr(dc, "click_xy", fake_click)
    return calls


def _type_recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    typed: list[str] = []

    async def fake_type(text: str) -> None:
        typed.append(text)

    monkeypatch.setattr(dc, "type_text", fake_type)
    return typed


# --- _core -------------------------------------------------------------------


def test_core_strips_action_filler() -> None:
    assert _core("click the Search menu") == "search"
    assert _core("press Save button") == "save"
    assert _core("please open the Settings tab for me") == "settings"


# --- _score / best_match -----------------------------------------------------


def test_score_exact_label_wins() -> None:
    assert _score("save", _el("Save")) > _score("save", _el("Save As"))


def test_score_one_of_target_words() -> None:
    # "save file" → element text "file" is one of the target words.
    assert _score("save file", _el("file")) >= 2.5


def test_score_substring_match() -> None:
    # target "settings" appears inside element text "Open Settings".
    s = _score("settings", _el("Open Settings"))
    assert 2.0 <= s < 2.5 + 1.0


def test_score_token_overlap() -> None:
    s = _score("export report", _el("Export"))
    assert s > 1.0  # shares one token, scored on the overlap rung


def test_score_no_match_is_zero() -> None:
    assert _score("save", _el("Quit")) == 0.0
    assert _score("", _el("Anything")) == 0.0


def test_best_match_picks_exact_over_partial() -> None:
    els = [_el("Save As"), _el("Save"), _el("File")]
    assert best_match("save", els).text == "Save"


def test_best_match_none_below_min_score() -> None:
    # Nothing shares a word with the target → below min_score → None.
    assert best_match("teleport", [_el("Save"), _el("File")]) is None


def test_best_match_higher_conf_breaks_ties() -> None:
    # Two equal-text candidates; the more confident one wins.
    low = _el("Save", conf=50.0)
    high = _el("Save", x=99, conf=99.0)
    assert best_match("save", [low, high]) is high


# --- act_on_screen -----------------------------------------------------------


async def test_act_empty_goal_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _ok_env(monkeypatch)
    clicks = _click_recorder(monkeypatch)
    out = await act_on_screen("   ")
    assert "error" in out
    assert clicks == []  # never clicked


async def test_act_no_ocr_available_errors_no_click(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dc, "ocr_available", lambda: False)
    monkeypatch.setattr(dc, "_XDOTOOL", "/usr/bin/xdotool")
    clicks = _click_recorder(monkeypatch)
    out = await act_on_screen("click Save")
    assert "error" in out
    assert clicks == []


async def test_act_ocr_raising_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _ok_env(monkeypatch)
    clicks = _click_recorder(monkeypatch)

    async def boom(*a: object, **k: object) -> list[Element]:
        raise RuntimeError("display gone")

    monkeypatch.setattr(dc, "_ocr_elements", boom)
    out = await act_on_screen("click Save")
    assert "error" in out
    assert clicks == []


async def test_act_no_text_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    _ok_env(monkeypatch)
    _ocr_returns(monkeypatch, [])
    out = await act_on_screen("click Save")
    assert "error" in out
    assert "any text controls" in out["error"]


async def test_act_clean_match_clicks_centre(monkeypatch: pytest.MonkeyPatch) -> None:
    _ok_env(monkeypatch)
    el = _el("Save", x=100, y=50, w=40, h=20, conf=95.0)
    _ocr_returns(monkeypatch, [el, _el("File", x=0, y=0)])
    clicks = _click_recorder(monkeypatch)
    out = await act_on_screen("click Save")
    assert out["acted"] is True
    assert out["target"] == "Save"
    assert out["matched_by"] == "label"
    assert clicks == [(el.cx, el.cy)]  # clicked the element's real centre
    assert out["coords"] == [el.cx, el.cy]


async def test_act_llm_fallback_when_no_literal_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _ok_env(monkeypatch)
    target = _el("Save", x=10, y=10, w=20, h=10)
    _ocr_returns(monkeypatch, [_el("Quit"), target])
    clicks = _click_recorder(monkeypatch)

    async def fake_pick(goal: str, elements: list[Element]) -> Element | None:
        return target  # semantic model resolves "store my work" → the Save element

    monkeypatch.setattr(dc, "_llm_pick", fake_pick)
    out = await act_on_screen("store my work")
    assert out["acted"] is True
    assert out["matched_by"] == "llm"
    assert clicks == [(target.cx, target.cy)]


async def test_act_no_match_anywhere_is_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    _ok_env(monkeypatch)
    _ocr_returns(monkeypatch, [_el("Quit"), _el("Help")])
    clicks = _click_recorder(monkeypatch)

    async def no_pick(goal: str, elements: list[Element]) -> Element | None:
        return None  # LLM also declines

    monkeypatch.setattr(dc, "_llm_pick", no_pick)
    out = await act_on_screen("teleport")
    assert out["acted"] is False
    assert "Quit" in out["note"] and "Help" in out["note"]  # lists what it can see
    assert clicks == []


async def test_act_llm_pick_degrades_to_honest_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real _llm_pick must swallow router failures and return None → honest no-match.
    # Mock the router's complete() to raise (no key / offline) and assert it's caught.
    _ok_env(monkeypatch)
    _ocr_returns(monkeypatch, [_el("Quit")])
    clicks = _click_recorder(monkeypatch)
    from friday.core import router

    async def boom(*a: object, **k: object) -> object:
        raise RuntimeError("no API key / offline")

    monkeypatch.setattr(router, "complete", boom)
    out = await act_on_screen("teleport")  # _llm_pick runs for real, must not crash
    assert out["acted"] is False
    assert clicks == []


# --- type_into ---------------------------------------------------------------


async def test_type_into_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _ok_env(monkeypatch)
    field = _el("Email", x=200, y=80, w=120, h=24, conf=92.0)
    _ocr_returns(monkeypatch, [field])
    clicks = _click_recorder(monkeypatch)
    typed = _type_recorder(monkeypatch)
    out = await type_into("Email", "me@example.com")
    assert out == {
        "typed": True,
        "field": "Email",
        "into": "Email",
        "text": "me@example.com",
    }
    assert clicks == [(field.cx, field.cy)]  # clicked the field first
    assert typed == ["me@example.com"]


async def test_type_into_no_match_does_not_type(monkeypatch: pytest.MonkeyPatch) -> None:
    _ok_env(monkeypatch)
    _ocr_returns(monkeypatch, [_el("Quit"), _el("Help")])
    clicks = _click_recorder(monkeypatch)
    typed = _type_recorder(monkeypatch)

    async def no_pick(goal: str, elements: list[Element]) -> Element | None:
        return None

    monkeypatch.setattr(dc, "_llm_pick", no_pick)
    out = await type_into("Search box", "hello")
    assert out["acted"] is False  # honest no-match dict
    assert typed == []  # never typed
    assert clicks == []


# --- registration ------------------------------------------------------------


def test_skills_are_confirmation_gated() -> None:
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("act_on_screen").auto_confirm is False
    assert registry.get("type_into").auto_confirm is False
