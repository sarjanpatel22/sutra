"""Ambient collectors: cheap observers that aggregate raw OS signals.

Each collector polls cheaply (no per-event LLM) and, on drain(), rolls everything
it saw since the last drain into a few natural-language sentences for the memory
store. Raw events are never persisted — only the rolled-up summary.
"""

from __future__ import annotations

import subprocess
import time
from collections import Counter
from pathlib import Path

import psutil

from friday.ambient import config

_RUN = dict(capture_output=True, text=True, timeout=2)

# Prettier names for common process names seen in the active window.
_APP_NAMES = {
    "code": "VS Code", "code-oss": "VS Code", "chromium": "Chromium",
    "chrome": "Chrome", "firefox": "Firefox", "zsh": "Terminal", "bash": "Terminal",
    "tmux": "Terminal", "kitty": "Terminal", "alacritty": "Terminal",
    "gnome-terminal": "Terminal", "qterminal": "Terminal", "obsidian": "Obsidian",
    "slack": "Slack", "discord": "Discord", "nautilus": "Files",
}


def fmt_dur(seconds: float) -> str:
    s = int(seconds)
    h, m = divmod(s // 60, 60)
    if h and m:
        return f"{h}h{m}m"
    if h:
        return f"{h}h"
    return f"{m}m" if m else f"{s}s"


def _and_list(items: list[str]) -> str:
    items = items[:6]
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


class Collector:
    name = "base"

    def poll(self, *, idle: bool) -> None:  # cheap, called every few seconds
        ...

    def drain(self) -> list[str]:  # summarize since last drain, reset state
        return []


# ── App / window focus ─────────────────────────────────────────────────────────

def _active_window() -> tuple[str, str] | None:
    """(app, title) of the focused X11 window, or None."""
    try:
        wid = subprocess.run(["xdotool", "getactivewindow"], **_RUN).stdout.strip()
        if not wid:
            return None
        title = subprocess.run(["xdotool", "getwindowname", wid], **_RUN).stdout.strip()
        pid = subprocess.run(["xdotool", "getwindowpid", wid], **_RUN).stdout.strip()
        app = "unknown"
        if pid.isdigit():
            try:
                proc = psutil.Process(int(pid)).name().lower()
                app = _APP_NAMES.get(proc, proc)
            except Exception:  # noqa: BLE001
                pass
        return app, title
    except Exception:  # noqa: BLE001
        return None


class WindowFocusCollector(Collector):
    name = "window"

    def __init__(self) -> None:
        self._dur: Counter[str] = Counter()
        self._titles: dict[str, Counter[str]] = {}

    def poll(self, *, idle: bool) -> None:
        if idle:
            return
        win = _active_window()
        if not win:
            return
        app, title = win
        self._dur[app] += config.POLL_INTERVAL_S
        if title:
            self._titles.setdefault(app, Counter())[title[:60]] += 1

    def drain(self) -> list[str]:
        out: list[str] = []
        for app, secs in self._dur.most_common():
            if secs < 60:  # ignore brief flickers
                continue
            top = self._titles.get(app)
            hint = f" (mostly on {top.most_common(1)[0][0]})" if top else ""
            out.append(f"Sarjan spent about {fmt_dur(secs)} in {app}{hint}.")
        self._dur.clear()
        self._titles.clear()
        return out


# ── File edits in watched folders ──────────────────────────────────────────────

class FileActivityCollector(Collector):
    name = "files"

    def __init__(self) -> None:
        self._since = time.time()

    def _recent_by_dir(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for root in config.WATCH_DIRS:
            if not root.exists():
                continue
            names: list[tuple[float, str]] = []
            for dirpath, dirnames, filenames in __import__("os").walk(root):
                dirnames[:] = [d for d in dirnames if d not in config.IGNORE_PARTS]
                for fn in filenames:
                    p = Path(dirpath) / fn
                    if not config.should_track_file(p):
                        continue
                    try:
                        mt = p.stat().st_mtime
                    except OSError:
                        continue
                    if mt >= self._since:
                        names.append((mt, fn))
            if names:
                names.sort(reverse=True)
                groups[root.name] = [n for _, n in names]
        return groups

    def drain(self) -> list[str]:
        groups = self._recent_by_dir()
        self._since = time.time()
        out = []
        for proj, files in groups.items():
            uniq = list(dict.fromkeys(files))
            out.append(f"Sarjan edited {_and_list(uniq)} in the {proj} project.")
        return out


# ── Git / project activity ─────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], **_RUN).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


class GitActivityCollector(Collector):
    name = "git"

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}  # repo -> last commit hash

    def _repos(self) -> list[Path]:
        return [d for d in config.WATCH_DIRS if (d / ".git").exists()]

    def drain(self) -> list[str]:
        out = []
        for repo in self._repos():
            head = _git(repo, "rev-parse", "HEAD")
            if not head or self._seen.get(str(repo)) == head:
                self._seen[str(repo)] = head
                continue
            first = str(repo) not in self._seen
            self._seen[str(repo)] = head
            if first:
                continue  # don't replay history on first run
            subject = _git(repo, "log", "-1", "--format=%s")
            branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
            out.append(f"Sarjan committed \"{subject}\" to {repo.name} on the {branch} branch.")
        return out


# ── Shell commands (summarized by command name only — args never stored) ────────

class ShellHistoryCollector(Collector):
    name = "shell"

    def __init__(self, hist: Path | None = None) -> None:
        self._path = hist or (config.HOME / ".zsh_history")
        try:
            self._offset = self._path.stat().st_size
        except OSError:
            self._offset = 0

    @staticmethod
    def base_command(line: str) -> str:
        """Extract the base command from a zsh history line; '' if none."""
        if ";" in line and line.startswith(":"):
            line = line.split(";", 1)[1]  # strip extended-history ': ts:dur;' prefix
        line = line.strip()
        # follow through sudo/env-prefix to the real command
        for tok in line.split():
            t = tok.strip()
            if not t or "=" in t or t in ("sudo", "env", "command", "time"):
                continue
            return t.rsplit("/", 1)[-1]  # basename, drop any path
        return ""

    def drain(self) -> list[str]:
        try:
            size = self._path.stat().st_size
            if size < self._offset:  # history rotated/truncated
                self._offset = 0
            with self._path.open("r", errors="ignore") as f:
                f.seek(self._offset)
                chunk = f.read()
                self._offset = f.tell()
        except OSError:
            return []
        cmds = Counter(
            c for c in (self.base_command(ln) for ln in chunk.splitlines()) if c
        )
        if not cmds:
            return []
        top = [c for c, _ in cmds.most_common(6)]
        return [f"Sarjan ran {_and_list(top)} commands in the terminal."]


_REGISTRY = {
    "window": WindowFocusCollector,
    "files": FileActivityCollector,
    "git": GitActivityCollector,
    "shell": ShellHistoryCollector,
}


def build_collectors() -> list[Collector]:
    """Instantiate the collectors enabled in config.SOURCES."""
    return [cls() for key, cls in _REGISTRY.items() if key in config.SOURCES]
