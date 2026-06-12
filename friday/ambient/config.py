"""Configuration + privacy guardrails for the ambient OS-memory daemon.

Everything the daemon is allowed to watch is gated here: an allowlist of folders,
a denylist of sensitive paths, ignore patterns for noise, and the cadence knobs.
All overridable via env so the user can dial any source back without code changes.
"""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, "1" if default else "0").lower() not in ("0", "false", "no")


def _watch_dirs() -> list[Path]:
    """Allowlisted folders to observe. Default: common project dirs that exist."""
    raw = os.getenv("AMBIENT_WATCH_DIRS")
    if raw:
        return [Path(p).expanduser() for p in raw.split(":") if p.strip()]
    candidates = [HOME / "friday-os", HOME / "projects", HOME / "Documents"]
    return [p for p in candidates if p.exists()]


# Master switch + per-source toggles (user picked "all"; shell included w/ redaction).
ENABLED = _flag("AMBIENT_ENABLED", True)
SOURCES = set(os.getenv("AMBIENT_SOURCES", "window,files,git,shell").split(","))

WATCH_DIRS: list[Path] = _watch_dirs()

# Cadence: poll cheaply, flush (summarize + write to memory) far less often.
POLL_INTERVAL_S = int(os.getenv("AMBIENT_POLL_S", "5"))
FLUSH_INTERVAL_S = int(os.getenv("AMBIENT_FLUSH_S", str(20 * 60)))  # 20 min
IDLE_THRESHOLD_S = int(os.getenv("AMBIENT_IDLE_S", "120"))  # AFK after 2 min no input

# Graph hygiene: auto-purge edge-level dialogue-narration facts each flush (see
# store.looks_like_narration_fact). OFF by default — deletion stays preview-first
# (`scripts/clean_memory.py --narration`); flip on once you trust the classifier.
SWEEP_NARRATION = _flag("AMBIENT_SWEEP_NARRATION", False)

# --- Noise filters (file activity) ---
IGNORE_PARTS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
    ".ruff_cache", ".pytest_cache", ".cache", "dist", "build", ".next", "target",
    "site-packages", ".egg-info",
}
IGNORE_SUFFIXES = {
    ".pyc", ".pyo", ".log", ".tmp", ".temp", ".swp", ".swo", ".lock", ".part",
    ".crdownload", ".o", ".class",
}

# --- Privacy denylist: never read/record anything under or named like these ---
SENSITIVE_PARTS = {
    ".ssh", ".gnupg", ".password-store", "keyrings", ".aws", ".kube", ".docker",
}
SENSITIVE_NAME_HINTS = (
    "secret", "password", "passwd", "credential", "token", "private", "id_rsa",
    ".env", ".pem", ".key",
)


def is_sensitive(path: Path) -> bool:
    """True if a path looks secret and must never be touched by the daemon."""
    parts = {p.lower() for p in path.parts}
    if parts & SENSITIVE_PARTS:
        return True
    name = path.name.lower()
    return any(h in name for h in SENSITIVE_NAME_HINTS)


def is_noise(path: Path) -> bool:
    """True for build/cache/junk files we don't care to remember."""
    if any(p in IGNORE_PARTS for p in path.parts):
        return True
    return path.suffix.lower() in IGNORE_SUFFIXES


def should_track_file(path: Path) -> bool:
    return not is_sensitive(path) and not is_noise(path)
