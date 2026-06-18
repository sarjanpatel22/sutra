"""Launch desktop GUI applications by friendly name (Firefox, terminal, editor, files).

This is for opening a desktop APPLICATION — NOT for opening a website. To hand a
URL to the browser use open_url / browser_open instead. Resolution never runs an
arbitrary shell: a friendly name is mapped to candidate commands, and only a real
executable (shutil.which) or a .desktop entry (gtk-launch) is ever launched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
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
    "text editor": ["gedit", "kate", "mousepad", "featherpad"],
    "editor": ["gedit", "kate", "mousepad", "featherpad"],
    # "notepad" is a Windows name with no Linux app of its own — map the spoken word
    # to whatever real editor exists, so "open notepad" actually opens something.
    "notepad": ["mousepad", "gedit", "kate", "featherpad", "leafpad", "xed"],
    "note": ["mousepad", "gedit", "kate", "featherpad", "leafpad", "xed"],
    "notes": ["mousepad", "gedit", "kate", "featherpad", "leafpad", "xed"],
}


def _desktop_id_for(name: str) -> str | None:
    """Find a real .desktop entry id matching `name`, or None if none exists.

    Scans the XDG application dirs so the gtk-launch fallback only fires for an app
    that genuinely exists — otherwise gtk-launch silently no-ops while we'd wrongly
    report success (the live 'Opening notepad' that opened nothing). Matches either
    the entry id (filename minus .desktop) or its Name= field, case-insensitively.
    """
    key = name.strip().lower()
    if not key:
        return None
    data_dirs = [Path.home() / ".local" / "share"]
    raw = os.getenv("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    data_dirs += [Path(p) for p in raw.split(":") if p]
    for base in data_dirs:
        apps = base / "applications"
        if not apps.is_dir():
            continue
        for entry in apps.glob("*.desktop"):
            if entry.stem.lower() == key:
                return entry.stem
            try:
                for line in entry.read_text(errors="ignore").splitlines():
                    if line.startswith("Name=") and line[5:].strip().lower() == key:
                        return entry.stem
            except OSError:
                continue
    return None


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

    # Fallback: let the desktop resolve a .desktop entry by id — but ONLY if one
    # really exists. gtk-launch on a missing id exits non-zero and opens nothing,
    # yet Popen still "succeeds", so we must verify first or we'd report a launch
    # that never happened (the live 'Opening notepad' that did nothing).
    if shutil.which("gtk-launch"):
        desktop_id = _desktop_id_for(name) or _desktop_id_for(_normalize(name))
        if desktop_id:
            tried.append(f"gtk-launch {desktop_id}")
            try:
                subprocess.Popen(
                    ["gtk-launch", desktop_id],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:  # noqa: BLE001 — never raise into the agent loop
                return {"error": f"failed to launch {name!r}: {exc}", "tried": tried}
            return {"status": "launched", "app": name, "command": f"gtk-launch {desktop_id}"}

    return {"error": f"couldn't find an app called {name!r} on this system", "tried": tried}
