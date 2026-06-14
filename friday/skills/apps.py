"""Launch desktop GUI applications by friendly name (Firefox, terminal, editor, files).

This is for opening a desktop APPLICATION — NOT for opening a website. To hand a
URL to the browser use open_url / browser_open instead. Resolution never runs an
arbitrary shell: a friendly name is mapped to candidate commands, and only a real
executable (shutil.which) or a .desktop entry (gtk-launch) is ever launched.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from friday.skills.registry import skill

# Friendly name -> ordered candidate commands. First one found on PATH wins.
ALIASES: dict[str, list[str]] = {
    "firefox": ["firefox"],
    "chrome": ["google-chrome", "chromium", "chromium-browser"],
    "chromium": ["chromium", "chromium-browser"],
    "browser": ["firefox", "google-chrome", "chromium", "chromium-browser"],
    "terminal": ["x-terminal-emulator", "konsole", "gnome-terminal", "xterm", "kitty",
                 "alacritty"],
    "code": ["code"],
    "vscode": ["code"],
    "vs code": ["code"],
    "files": ["nautilus", "dolphin", "thunar", "pcmanfm"],
    "file manager": ["nautilus", "dolphin", "thunar", "pcmanfm"],
    "calculator": ["gnome-calculator", "kcalc"],
    "calc": ["gnome-calculator", "kcalc"],
    "text editor": ["gedit", "kate", "mousepad"],
    "editor": ["gedit", "kate", "mousepad"],
}


# Apps that, when already running, just refocus instead of showing a window — force a
# fresh visible window so "open Firefox" always does something the user can see.
_NEW_WINDOW_FLAG: dict[str, list[str]] = {
    "firefox": ["--new-window"],
    "google-chrome": ["--new-window"],
    "chromium": ["--new-window"],
    "chromium-browser": ["--new-window"],
}


def _normalize(name: str) -> str:
    """Lower-case and strip filler words so 'the Firefox app' -> 'firefox'."""
    n = name.strip().lower()
    for prefix in ("the ", "my "):
        if n.startswith(prefix):
            n = n[len(prefix):]
    if n.endswith(" app"):
        n = n[: -len(" app")]
    return n.strip()


def _candidates(name: str) -> list[str]:
    """Candidate commands for a friendly name: alias match, else the name itself."""
    key = _normalize(name)
    if key in ALIASES:
        return ALIASES[key]
    return [key] if key else []


# NOTE: "open_app" starts with the "open" mutation verb, so the integrator must add
# it to audit.REVIEWED_AUTO_EXCEPTIONS. Do NOT edit audit.py here.
@skill(
    name="open_app",
    description=(
        "THE tool to open or launch a desktop application by name — e.g. 'open firefox', "
        "'launch terminal', 'open vs code', 'open calculator'. Pass the app name (e.g. "
        "'firefox'). For a website/URL use open_url; this starts a program, not a web page."
    ),
    auto_confirm=True,
    timeout_s=20,
)
async def open_app(name: str) -> dict[str, Any]:
    """Launch a desktop application by friendly name.

    name: Friendly app name, e.g. 'firefox', 'terminal', 'text editor'.
    """
    tried: list[str] = []
    for cmd in _candidates(name):
        tried.append(cmd)
        path = shutil.which(cmd)
        if path:
            flags = _NEW_WINDOW_FLAG.get(cmd, [])
            try:
                subprocess.Popen(
                    [path, *flags],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:  # noqa: BLE001 — never raise into the agent loop
                return {"error": f"failed to launch {cmd!r}: {exc}", "tried": tried}
            return {"status": "launched", "app": name, "command": " ".join([cmd, *flags])}

    # Fallback: let the desktop resolve a .desktop entry by id.
    if shutil.which("gtk-launch"):
        tried.append("gtk-launch")
        try:
            subprocess.Popen(
                ["gtk-launch", _normalize(name)],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001 — never raise into the agent loop
            return {"error": f"failed to launch {name!r}: {exc}", "tried": tried}
        return {"status": "launched", "app": name, "command": f"gtk-launch {_normalize(name)}"}

    return {"error": f"couldn't find an app called {name!r} on this system", "tried": tried}
