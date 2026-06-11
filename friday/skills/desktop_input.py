"""Gated desktop-input skills: synthesize keyboard/mouse input via xdotool (X11).

These emit real input into whatever window currently has focus — they can type
commands, press shortcuts, click anything — so every input-emitting skill is
auto_confirm=False (the agent confirms first; a sub-agent declines them), exactly
like run_command / fill_form. `active_window` is the one read-only query and auto-runs.

Reuses desktop.py's subprocess runner. X11 only (needs DISPLAY); returns a clean
error dict when xdotool is missing rather than raising.
"""

from __future__ import annotations

import shutil
from typing import Any

from friday.skills.desktop import _run
from friday.skills.registry import skill


def _no_xdotool() -> dict[str, Any] | None:
    """Error dict if xdotool is unavailable, else None."""
    if not shutil.which("xdotool"):
        return {"error": "xdotool not installed (X11 input automation unavailable)"}
    return None


@skill(
    name="active_window",
    description="Report the currently focused window's id, title, and geometry (read-only).",
    auto_confirm=True,
    timeout_s=5,
)
async def active_window() -> dict[str, Any]:
    """Return the focused window's id, title, and geometry."""
    if err := _no_xdotool():
        return err
    code, wid, msg = await _run(["xdotool", "getactivewindow"])
    if code != 0 or not wid.strip():
        return {"error": msg.strip() or "no active window"}
    wid = wid.strip()
    _, name, _ = await _run(["xdotool", "getwindowname", wid])
    _, geo, _ = await _run(["xdotool", "getwindowgeometry", wid])
    return {"window_id": wid, "name": name.strip(), "geometry": " ".join(geo.split())}


@skill(
    name="type_text",
    description=(
        "Type a string of text into the focused window, as if typed on the keyboard. "
        "IRREVERSIBLE (goes wherever focus is) — requires confirmation."
    ),
    auto_confirm=False,
    timeout_s=15,
)
async def type_text(text: str) -> dict[str, Any]:
    """Type text into the active window.

    text: The literal text to type.
    """
    if not text:
        return {"error": "no text to type"}
    if err := _no_xdotool():
        return err
    code, _, msg = await _run(["xdotool", "type", "--clearmodifiers", "--", text])
    if code != 0:
        return {"error": msg.strip() or "type failed"}
    return {"status": "ok", "typed_chars": len(text)}


@skill(
    name="press_key",
    description=(
        "Press a key or chord in the focused window (e.g. 'Return', 'ctrl+s', "
        "'alt+Tab'); space-separate for a sequence ('ctrl+a Delete'). IRREVERSIBLE "
        "— requires confirmation."
    ),
    auto_confirm=False,
    timeout_s=10,
)
async def press_key(keys: str) -> dict[str, Any]:
    """Press one or more keys/chords in the active window.

    keys: An xdotool keysym or chord ("Return", "ctrl+s"); space-separated for a sequence.
    """
    keys = keys.strip()
    if not keys:
        return {"error": "no keys to press"}
    if err := _no_xdotool():
        return err
    code, _, msg = await _run(["xdotool", "key", "--clearmodifiers", *keys.split()])
    if code != 0:
        return {"error": msg.strip() or "key press failed", "keys": keys}
    return {"status": "ok", "keys": keys}


@skill(
    name="mouse_click",
    description=(
        "Click the mouse (button 1=left, 2=middle, 3=right) at an optional x,y screen "
        "position, else the current cursor. IRREVERSIBLE — requires confirmation."
    ),
    auto_confirm=False,
    timeout_s=10,
)
async def mouse_click(button: int = 1, x: int = -1, y: int = -1) -> dict[str, Any]:
    """Click the mouse, optionally moving to a screen position first.

    button: 1 (left), 2 (middle), or 3 (right).
    x: X screen coordinate to move to first; negative = keep current cursor X/Y.
    y: Y screen coordinate to move to first; negative = keep current cursor.
    """
    if button not in (1, 2, 3):
        return {"error": "button must be 1 (left), 2 (middle), or 3 (right)"}
    if err := _no_xdotool():
        return err
    if x >= 0 and y >= 0:
        await _run(["xdotool", "mousemove", str(x), str(y)])
    code, _, msg = await _run(["xdotool", "click", str(button)])
    if code != 0:
        return {"error": msg.strip() or "click failed"}
    return {"status": "ok", "button": button, "moved": x >= 0 and y >= 0}
