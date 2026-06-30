"""Tests for the find_on_screen skill (friday/skills/screen_perception.py).

perceive.grab is mocked to return synthetic frames, so the skill tests headless (no real screen).
"""

from __future__ import annotations

import shutil

import cv2
import numpy as np
import pytest

from friday.skills import screen_perception
from friday.vision import perceive

_NEEDS_OCR = pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")


def _frame_with_text(text: str) -> np.ndarray:
    img = np.full((140, 500, 3), 30, np.uint8)
    cv2.putText(img, text, (40, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (255, 255, 255), 2)
    return img


@_NEEDS_OCR
async def test_finds_text_and_returns_coords(monkeypatch):
    monkeypatch.setattr(perceive, "grab", lambda region=None: _frame_with_text("DOWNLOAD"))
    out = await screen_perception.find_on_screen(target="download")
    assert out["found"] is True and out["method"] == "text"
    assert 40 <= out["x"] <= 480 and 20 <= out["y"] <= 130
    assert out["box"] and out["score"] >= 0


async def test_not_found_reports_element_count(monkeypatch):
    def frame(region=None):
        img = np.zeros((300, 400, 3), np.uint8)
        for x, y, w, h in [(20, 20, 120, 30), (20, 90, 120, 30)]:
            cv2.rectangle(img, (x, y), (x + w, y + h), (255, 255, 255), 2)
        return img

    monkeypatch.setattr(perceive, "grab", frame)
    out = await screen_perception.find_on_screen(target="nonexistent")
    assert out["found"] is False
    assert out["clickable_elements"] >= 2  # the drawn rectangles


async def test_template_path_branch(monkeypatch, tmp_path):
    frame = _frame_with_text("HELLO")
    monkeypatch.setattr(perceive, "grab", lambda region=None: frame)
    tpl_path = str(tmp_path / "tpl.png")
    cv2.imwrite(tpl_path, frame[40:110, 30:300])  # a slice of the frame as the template
    out = await screen_perception.find_on_screen(template_path=tpl_path)
    assert out["found"] is True and out["method"] == "template"


async def test_bad_template_path(monkeypatch):
    monkeypatch.setattr(perceive, "grab", lambda region=None: _frame_with_text("X"))
    out = await screen_perception.find_on_screen(template_path="/no/such/image.png")
    assert "error" in out


async def test_grab_failure_is_graceful(monkeypatch):
    def boom(region=None):
        raise RuntimeError("no display")

    monkeypatch.setattr(perceive, "grab", boom)
    out = await screen_perception.find_on_screen(target="x")
    assert "error" in out and "capture" in out["error"]


async def test_no_target():
    assert "error" in await screen_perception.find_on_screen()


def test_find_on_screen_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("find_on_screen").auto_confirm is True  # read-only perception
