"""Desktop screen capture + cheap change-detection — the eyes for `sutra watch`
and the `guide_me` skill.

Uses ImageMagick `import` (the only screenshot tool present on this Kali box —
scrot/maim/grim are all absent) to grab the X11 root window, so capture works with
zero Python imaging dependencies. Two outputs:

  • grab_jpeg()  — a downscaled JPEG for the vision model (small, fast upload).
  • ahash()      — an 8x8 grayscale average-hash so callers can ask "did the screen
                   meaningfully change?" without burning a vision call on every tick.

Everything degrades honestly: with no DISPLAY or no `import`, capture raises
ScreenUnavailable and the hash returns 0 ("unknown") rather than crashing a loop.
"""

from __future__ import annotations

import asyncio
import os
import shutil

_IMPORT = shutil.which("import")  # ImageMagick's screenshot tool


class ScreenUnavailable(RuntimeError):
    """No display or no screenshot tool — capture can't run in this environment."""


def screen_available() -> bool:
    """True if we can actually grab the screen here (X11 display + ImageMagick)."""
    return bool(_IMPORT) and bool(os.getenv("DISPLAY"))


async def _run(cmd: list[str]) -> bytes:
    """Run a capture command, returning its stdout bytes or raising ScreenUnavailable."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await proc.communicate()
    except Exception as e:  # noqa: BLE001 — spawn failure (no binary, no display)
        raise ScreenUnavailable(str(e)) from e
    if proc.returncode != 0 or not out:
        raise ScreenUnavailable(f"{cmd[0]} failed (rc={proc.returncode})")
    return out


async def grab_jpeg(quality: int = 70, max_width: int = 1280) -> bytes:
    """Full-screen JPEG bytes, downscaled so the long edge is <= max_width.

    Downscaling keeps the vision upload small/fast; the model reads layout and big
    controls fine at 1280px. Raises ScreenUnavailable off-display.
    """
    if not screen_available():
        raise ScreenUnavailable("no DISPLAY or ImageMagick 'import'")
    assert _IMPORT  # screen_available() guarantees the binary; narrows str | None -> str
    q = max(20, min(95, quality))
    return await _run(
        [_IMPORT, "-window", "root", "-resize", f"{max_width}>", "-quality", str(q), "jpg:-"]
    )


async def ahash() -> int:
    """8x8 grayscale average-hash (64-bit int) for cheap change detection.

    Insensitive to a ticking clock or a blinking cursor (those don't shift the 8x8
    average), sensitive to switching windows, scrolling, or opening a dialog. Returns
    0 if capture fails, so the caller treats it as 'unknown' instead of crashing.
    """
    # Narrow _IMPORT (str | None) to str without short-circuiting: if the binary is absent
    # _run still runs and raises ScreenUnavailable -> 0 (and stays monkeypatchable in tests).
    imp = _IMPORT or "import"
    try:
        raw = await _run(
            [imp, "-window", "root", "-resize", "8x8!", "-colorspace", "Gray",
             "-depth", "8", "gray:-"]
        )
    except ScreenUnavailable:
        return 0
    px = raw[:64]
    if len(px) < 64:
        return 0
    avg = sum(px) / len(px)
    bits = 0
    for i, v in enumerate(px):
        if v >= avg:
            bits |= 1 << i
    return bits


def hamming(a: int, b: int) -> int:
    """Bit-difference between two ahash values (0 = identical, up to 64 = inverse)."""
    return bin(a ^ b).count("1")


def changed(prev: int, cur: int, threshold: int = 6) -> bool:
    """True if the screen changed meaningfully since `prev`.

    No baseline (prev == 0) or a failed current capture (cur == 0) counts as changed,
    so the first tick and any capture hiccup err toward looking rather than going mute.
    """
    if not prev or not cur:
        return True
    return hamming(prev, cur) >= threshold
