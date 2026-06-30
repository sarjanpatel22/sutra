"""Tests for fast screen perception (friday/vision/perceive.py).

All synthetic images — the detection functions are pure (no live screen), so these run headless.
grab() (the only screen-touching function) is not exercised here.
"""

from __future__ import annotations

import shutil

import cv2
import numpy as np
import pytest

from friday.vision import perceive as P

_NEEDS_OCR = pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")


def test_find_template_locates_patch():
    img = np.zeros((200, 300, 3), np.uint8)
    cv2.rectangle(img, (50, 40), (110, 80), (255, 255, 255), -1)
    cv2.line(img, (55, 45), (105, 75), (0, 0, 0), 3)  # texture so the template has variance
    cv2.circle(img, (80, 60), 8, (0, 0, 0), -1)
    cv2.rectangle(img, (180, 120), (210, 150), (160, 160, 160), -1)  # decoy
    tpl = img[40:80, 50:110].copy()
    m = P.find_template(img, tpl, threshold=0.9)
    assert m is not None
    assert abs(m.x - 50) <= 2 and abs(m.y - 40) <= 2
    assert m.center == (m.x + m.w // 2, m.y + m.h // 2)


def test_find_template_absent_returns_none():
    img = np.zeros((100, 100, 3), np.uint8)
    tpl = np.zeros((20, 20, 3), np.uint8)
    cv2.line(tpl, (0, 0), (19, 19), (255, 255, 255), 2)  # textured patch not present in the image
    assert P.find_template(img, tpl, threshold=0.9) is None


def test_find_template_too_big_returns_none():
    img = np.zeros((40, 40, 3), np.uint8)
    tpl = np.zeros((80, 80, 3), np.uint8)
    assert P.find_template(img, tpl) is None


def test_detect_elements_finds_rectangles():
    img = np.zeros((300, 400, 3), np.uint8)
    for x, y, w, h in [(20, 20, 120, 30), (20, 90, 120, 30), (220, 160, 90, 40)]:
        cv2.rectangle(img, (x, y), (x + w, y + h), (255, 255, 255), 2)
    els = P.detect_elements(img)
    assert len(els) >= 3  # the three button-like rectangles (overlaps suppressed)
    assert all(e.w >= 24 and e.h >= 12 for e in els)


@_NEEDS_OCR
def test_locate_text_reads_and_locates():
    img = np.full((120, 400, 3), 30, np.uint8)
    cv2.putText(img, "DOWNLOAD", (40, 75), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (255, 255, 255), 2)
    m = P.locate_text(img, "download")
    assert m is not None
    cx, cy = m.center
    assert 40 <= cx <= 380 and 20 <= cy <= 110


@_NEEDS_OCR
def test_locate_text_region_offset_is_absolute():
    img = np.full((300, 600, 3), 30, np.uint8)
    cv2.putText(img, "SUBMIT", (380, 250), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (255, 255, 255), 2)
    m = P.locate_text(img, "submit", region=(300, 150, 300, 150))  # crop bottom-right quadrant
    assert m is not None and m.x >= 300 and m.y >= 150  # region offset added back


def test_locate_text_missing_returns_none():
    img = np.full((80, 200, 3), 30, np.uint8)
    cv2.putText(img, "HELLO", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
    assert P.locate_text(img, "nonexistent") is None


def test_changed_gate():
    a = np.zeros((50, 50, 3), np.uint8)
    assert P.changed(a, a.copy()) is False
    b = a.copy()
    b[0:25, 0:25] = 255
    assert P.changed(a, b) is True
    assert P.changed(a, np.zeros((10, 10, 3), np.uint8)) is True  # shape mismatch
    assert P.changed(None, a) is True
