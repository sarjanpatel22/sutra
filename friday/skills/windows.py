"""Window-management skills: list, focus, and close open windows via xdotool (X11).

Lets the user manage their open desktop windows by voice:
  list_windows  — what windows are currently open (read-only)
  focus_window  — switch to / bring up an already-open window by title
  close_window  — close an open window by title (gated; can lose unsaved work)

Reuses desktop.py's subprocess runner. X11 only (needs DISPLAY); every skill
returns a clean error dict when xdotool is missing rather than raising. Window
title matching is done in Python (substring, case-insensitive) rather than
trusting xdotool's own name matching, which is fuzzy and unreliable.
"""

from __future__ import annotations

import shutil
from typing import Any

from friday.skills.desktop import _run
from friday.skills.registry import skill

_MAX_WINDOWS = 30


def _no_xdotool() -> dict[str, Any] | None:
    """Error dict if xdotool is unavailable, else None."""
    if not shutil.which("xdotool"):
        return {"error": "xdotool not installed (X11 window management unavailable)"}
    return None


async def _scan_windows() -> list[dict[str, Any]]:
    """Return visible top-level windows as [{"id": str, "title": str}, ...].

    Gets ids via `xdotool search --onlyvisible --name "."`, then resolves each
    title with `xdotool getwindowname <id>`. Windows with empty titles are skipped.
    """
    code, out, _ = await _run(["xdotool", "search", "--onlyvisible", "--name", "."])
    if code != 0:
        return []
    windows: list[dict[str, Any]] = []
    for wid in out.split():
        if len(windows) >= _MAX_WINDOWS:
            break
        name_code, name, _ = await _run(["xdotool", "getwindowname", wid])
        title = name.strip()
        if name_code != 0 or not title:
            continue
        windows.append({"id": wid, "title": title})
    return windows


def _match(windows: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
    """First window whose title contains `title` (case-insensitive), else None."""
    needle = title.strip().lower()
    for win in windows:
        if needle in win["title"].lower():
            return win
    return None


@skill(
    name="list_windows",
    description=(
        "List the visible top-level windows currently open on the desktop, with their "
        "titles. Use this to see what's open. Read-only."
    ),
    auto_confirm=True,
    timeout_s=10,
)
async def list_windows() -> dict[str, Any]:
    """Return the open visible windows as {"windows": [{"id", "title"}, ...]}."""
    if err := _no_xdotool():
        return err
    return {"windows": await _scan_windows()}


@skill(
    name="focus_window",
    description=(
        "Switch to / bring up an already-open window: raise and focus the first visible "
        "window whose title contains the given text (case-insensitive)."
    ),
    auto_confirm=True,
    timeout_s=10,
)
async def focus_window(title: str) -> dict[str, Any]:
    """Raise and focus the first window matching a title substring.

    title: Text to match against open window titles (case-insensitive substring).
    """
    if not title.strip():
        return {"error": "no window title to match"}
    if err := _no_xdotool():
        return err
    match = _match(await _scan_windows(), title)
    if match is None:
        return {"error": f"no window matching '{title}'"}
    code, _, msg = await _run(["xdotool", "windowactivate", match["id"]])
    if code != 0:
        return {"error": msg.strip() or "failed to focus window", "title": match["title"]}
    return {"status": "focused", "title": match["title"]}


@skill(
    name="close_window",
    description=(
        "Close an open window: find the first visible window whose title contains the "
        "given text (case-insensitive) and close it. May lose unsaved work — confirmed."
    ),
    auto_confirm=False,
    timeout_s=10,
)
async def close_window(title: str) -> dict[str, Any]:
    """Close the first window matching a title substring.

    title: Text to match against open window titles (case-insensitive substring).
    """
    if not title.strip():
        return {"error": "no window title to match"}
    if err := _no_xdotool():
        return err
    match = _match(await _scan_windows(), title)
    if match is None:
        return {"error": f"no window matching '{title}'"}
    code, _, msg = await _run(["xdotool", "windowclose", match["id"]])
    if code != 0:
        return {"error": msg.strip() or "failed to close window", "title": match["title"]}
    return {"status": "closed", "title": match["title"]}
