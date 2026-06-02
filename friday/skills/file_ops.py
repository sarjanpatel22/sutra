"""File operations skill — read auto-confirmed, write requires confirmation."""

from __future__ import annotations

import os
from pathlib import Path

from friday.skills.registry import skill

_HOME = Path.home()


def _safe_path(path_str: str) -> Path:
    """Resolve path and ensure it stays within home directory."""
    p = Path(path_str).expanduser().resolve()
    try:
        p.relative_to(_HOME)
    except ValueError:
        raise PermissionError(f"Path outside home directory: {p}") from None
    return p


@skill(
    name="read_file",
    description="Read the contents of a file.",
    auto_confirm=True,
    timeout_s=5,
)
async def read_file(path: str, max_bytes: int = 65536) -> dict[str, str | int]:
    """Read a file from disk.

    path: Absolute or ~ -relative path to the file.
    max_bytes: Maximum bytes to read (default 64KB).
    """
    p = _safe_path(path)
    if not p.exists():
        return {"error": f"File not found: {p}"}
    if not p.is_file():
        return {"error": f"Not a file: {p}"}
    raw = p.read_bytes()
    truncated = len(raw) > max_bytes
    content = raw[:max_bytes]
    return {
        "content": content.decode(errors="replace"),
        "bytes_read": len(content),
        "truncated": str(truncated),
    }


@skill(
    name="write_file",
    description="Write content to a file (creates or overwrites).",
    auto_confirm=False,  # irreversible write — always confirm
    timeout_s=5,
)
async def write_file(path: str, content: str) -> dict[str, str]:
    """Write content to a file.

    path: Absolute or ~ -relative path to write.
    content: Text content to write.
    """
    p = _safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return {"status": "ok", "path": str(p), "bytes_written": str(len(content.encode()))}


@skill(
    name="list_directory",
    description="List files and directories at a given path.",
    auto_confirm=True,
    timeout_s=5,
)
async def list_directory(path: str = "~", pattern: str = "*") -> dict[str, list[str]]:
    """List a directory.

    path: Directory path to list (default: home).
    pattern: Glob pattern to filter entries (default: *).
    """
    p = _safe_path(path)
    if not p.is_dir():
        return {"error": [f"Not a directory: {p}"]}
    entries = sorted(str(e.relative_to(p)) for e in p.glob(pattern) if not e.name.startswith("."))
    return {"entries": entries[:200], "path": str(p)}
