"""Fast screen perception — mss capture + OpenCV template/element detection + region-scoped OCR.

The fast, reliable replacement for the slow `import` + full-screen-OCR path:
- CAPTURE: mss grabs frames into numpy at 30-60+ fps (vs ~1/sec with ImageMagick).
- TEMPLATE MATCH: find a KNOWN target image instantly with exact coords + confidence (OpenCV) —
  the reliable path for "click *this* button".
- ELEMENT DETECTION: find ARBITRARY clickable rectangles via contour analysis — no trained model
  (a deep-learning YOLO detector can drop in behind the same Match interface later; GPU is ready).
- OCR: tesseract scoped to a REGION, so it's fast and never picks up other windows' text.
- CHANGE: a cheap frame diff so a continuous watch loop only analyses what actually changed.

Detection functions are PURE (take a numpy image) so they test headless; only grab() touches the
live screen. Pure-stdlib + numpy/opencv/mss. $0.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass
class Match:
    """A located region on screen (top-left x,y + size), with a detection score."""

    x: int
    y: int
    w: int
    h: int
    score: float = 1.0

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


def grab(region: tuple[int, int, int, int] | None = None) -> np.ndarray:
    """Capture the screen (or a region: left, top, width, height) as a BGR numpy array via mss."""
    import mss  # imported lazily — only needed for a live grab, not for the pure detectors

    with mss.mss() as sct:
        mon = sct.monitors[1] if region is None else {
            "left": region[0], "top": region[1], "width": region[2], "height": region[3]}
        shot = sct.grab(mon)
    return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)


def _gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def find_template(image: np.ndarray, template: np.ndarray, threshold: float = 0.8) -> Match | None:
    """Best location of `template` within `image` (OpenCV normed cross-correlation). None if low."""
    img, tpl = _gray(image), _gray(template)
    if tpl.shape[0] > img.shape[0] or tpl.shape[1] > img.shape[1]:
        return None
    res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
    res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)  # uniform template → NaN; treat as 0
    _, maxv, _, loc = cv2.minMaxLoc(res)
    if maxv < threshold:
        return None
    h, w = tpl.shape[:2]
    return Match(int(loc[0]), int(loc[1]), int(w), int(h), float(maxv))


def _iou(a: Match, b: Match) -> float:
    ix1, iy1 = max(a.x, b.x), max(a.y, b.y)
    ix2, iy2 = min(a.x + a.w, b.x + b.w), min(a.y + a.h, b.y + b.h)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union else 0.0


def _nms(boxes: list[Match], thr: float = 0.6) -> list[Match]:
    kept: list[Match] = []
    for m in sorted(boxes, key=lambda b: b.w * b.h, reverse=True):
        if all(_iou(m, k) < thr for k in kept):
            kept.append(m)
    return kept


def detect_elements(image: np.ndarray, min_w: int = 24, min_h: int = 12,
                    max_frac: float = 0.7) -> list[Match]:
    """Find button/field-like rectangles via contour analysis — arbitrary clickable elements, no
    trained model. Filtered by size and a wide-ish aspect ratio; overlaps suppressed (NMS)."""
    g = _gray(image)
    edges = cv2.dilate(cv2.Canny(g, 50, 150), np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    big_h, big_w = g.shape[:2]
    out: list[Match] = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w < min_w or h < min_h:
            continue
        if w > big_w * max_frac and h > big_h * max_frac:
            continue  # whole-screen container, not an element
        ar = w / max(h, 1)
        if 1.1 <= ar <= 14:  # button/field-shaped
            out.append(Match(int(x), int(y), int(w), int(h), 1.0))
    return _nms(out)


def _preprocess(img: np.ndarray, scale: float) -> np.ndarray:
    """Upscale + binarize for OCR — small light-on-dark UI text is unreadable raw. Inverts so text
    ends up dark-on-light (what tesseract expects)."""
    g = _gray(img)
    g = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    _, th = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if float(np.mean(th)) < 127:  # mostly dark → invert to dark text on a light background
        th = cv2.bitwise_not(th)
    return th


def _ocr_tsv(img: np.ndarray, scale: float = 2.0) -> list[dict]:
    if not shutil.which("tesseract"):
        return []
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "f.png")
        cv2.imwrite(p, _preprocess(img, scale))
        out = subprocess.run(["tesseract", p, "stdout", "tsv"],
                             capture_output=True, text=True).stdout
    rows: list[dict] = []
    for ln in out.splitlines()[1:]:
        c = ln.split("\t")
        if len(c) == 12 and c[11].strip():  # map coords back from the upscaled image
            x, y = int(int(c[6]) / scale), int(int(c[7]) / scale)
            w, h = int(int(c[8]) / scale), int(int(c[9]) / scale)
            try:
                conf = float(c[10])
            except ValueError:
                conf = -1.0
            rows.append({"x": x, "y": y, "w": w, "h": h, "cx": x + w // 2, "cy": y + h // 2,
                         "conf": conf, "line": (c[2], c[3], c[4]), "t": c[11]})
    return rows


def ocr_words(image: np.ndarray, scale: float = 2.0) -> list[dict]:
    """Public OCR: every on-screen word with absolute coords, centre, and tesseract confidence.
    Preprocessed (upscale + binarize) so small light-on-dark UI text reads reliably."""
    return _ocr_tsv(image, scale)


def locate_text(image: np.ndarray, query: str,
                region: tuple[int, int, int, int] | None = None) -> Match | None:
    """Find `query` text in `image` via region-scoped OCR. Region offset is added back so the
    returned coords are absolute. Matches a consecutive run of the query's words on one line."""
    ox, oy = (region[0], region[1]) if region else (0, 0)
    crop = image[oy:oy + region[3], ox:ox + region[2]] if region else image
    rows = _ocr_tsv(crop)
    words = [w for w in query.lower().split() if w]
    if not words or not rows:
        return None
    for i in range(len(rows) - len(words) + 1):
        run = rows[i:i + len(words)]
        if all(run[j]["line"] == run[0]["line"] for j in range(len(run))) and all(
                words[j] in run[j]["t"].lower() for j in range(len(words))):
            x1 = min(r["x"] for r in run)
            y1 = min(r["y"] for r in run)
            x2 = max(r["x"] + r["w"] for r in run)
            y2 = max(r["y"] + r["h"] for r in run)
            return Match(x1 + ox, y1 + oy, x2 - x1, y2 - y1, 1.0)
    return None


def changed(prev: np.ndarray, cur: np.ndarray, threshold: float = 2.0) -> bool:
    """Cheap change gate: mean absolute pixel difference above `threshold` → the screen changed."""
    if prev is None or cur is None or prev.shape != cur.shape:
        return True
    return float(np.mean(cv2.absdiff(_gray(prev), _gray(cur)))) > threshold
