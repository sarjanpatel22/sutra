"""OCR-based on-screen text location — REAL coordinates for desktop control.

Why OCR instead of asking a vision model for pixel coordinates: a VLM is unreliable
at exact pixels ("somewhere around x=600") and a click that misses is worse than no
click. tesseract returns the exact bounding box of every piece of on-screen text, so
a click lands dead-centre on the real control. The model's only job is the easy,
reliable one — pick which *label* matches the goal; the geometry is ground truth.

Limitation, stated honestly: this finds TEXT-labelled controls (buttons, menus,
links, fields with visible text). Icon-only controls carry no text for OCR to read —
that's a real gap, not papered over. Full-resolution PNG is used (not the downscaled
JPEG the watch loop uses) because small UI text needs the detail to OCR cleanly.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass

_TESSERACT = shutil.which("tesseract")
_IMPORT = shutil.which("import")  # ImageMagick — full-res root-window grab for OCR


@dataclass(frozen=True)
class Element:
    """One piece of on-screen text and its real pixel bounding box."""

    text: str
    x: int  # left
    y: int  # top
    w: int
    h: int
    conf: float  # tesseract confidence 0–100

    @property
    def cx(self) -> int:
        """Centre x — where to click."""
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        """Centre y — where to click."""
        return self.y + self.h // 2


def ocr_available() -> bool:
    """True if the OCR + capture tools needed for desktop location are present."""
    return bool(_TESSERACT) and bool(_IMPORT)


def parse_tsv(tsv: str, min_conf: float = 40.0) -> list[Element]:
    """Parse tesseract TSV stdout into confident, non-empty text Elements.

    Pure function (no I/O) so the parsing is unit-testable with a canned TSV string.
    Drops empty tokens and anything below `min_conf` (OCR noise).
    """
    lines = tsv.splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    try:
        iL, iT, iW, iH, iC, iX = (
            header.index(k) for k in ("left", "top", "width", "height", "conf", "text")
        )
    except ValueError:
        return []
    out: list[Element] = []
    for ln in lines[1:]:
        cols = ln.split("\t")
        if len(cols) <= iX:
            continue
        text = cols[iX].strip()
        if not text:
            continue
        try:
            conf = float(cols[iC])
            x, y, w, h = (int(cols[i]) for i in (iL, iT, iW, iH))
        except ValueError:
            continue
        if conf < min_conf or w <= 0 or h <= 0:
            continue
        out.append(Element(text=text, x=x, y=y, w=w, h=h, conf=conf))
    return out


async def _run(cmd: list[str], stdin: bytes | None = None) -> bytes:
    """Run a subprocess, feeding optional stdin, returning stdout (raise on failure)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate(input=stdin)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed (rc={proc.returncode})")
    return out


async def grab_png_fullres() -> bytes:
    """Full-resolution, lossless PNG of the root window — OCR wants every pixel."""
    if not _IMPORT:
        raise RuntimeError("ImageMagick 'import' not installed")
    return await _run([_IMPORT, "-window", "root", "png:-"])


async def ocr_image(png: bytes, min_conf: float = 40.0) -> list[Element]:
    """OCR PNG/JPEG bytes → confident text elements with real bounding boxes.

    Uses --psm 11 (sparse text): find as much text as possible regardless of layout,
    which suits scattered UI labels better than the default page-segmentation mode.
    """
    if not _TESSERACT:
        raise RuntimeError("tesseract not installed")
    tsv = await _run([_TESSERACT, "-", "-", "--psm", "11", "tsv"], stdin=png)
    return parse_tsv(tsv.decode("utf-8", "replace"), min_conf)


async def ocr_screen(min_conf: float = 40.0) -> list[Element]:
    """Capture the screen at full resolution and OCR it into located Elements."""
    png = await grab_png_fullres()
    return await ocr_image(png, min_conf)
