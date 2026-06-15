"""File operations skill — read auto-confirmed, write requires confirmation."""

from __future__ import annotations

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
    description="Read a file's text contents — 'read/show/open the file X'. Use this, not "
    "run_command, to read a file.",
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
    name="edit_file",
    description=(
        "Edit a file by replacing an exact text snippet with new text — targeted, "
        "not a whole-file overwrite. The snippet must match exactly once (or pass "
        "replace_all). IRREVERSIBLE — requires confirmation."
    ),
    auto_confirm=False,  # mutates a file in place — always confirm
    timeout_s=5,
)
async def edit_file(
    path: str, old_text: str, new_text: str, replace_all: bool = False
) -> dict[str, str | int]:
    """Replace an exact snippet inside an existing file (the Cline/Continue pattern:
    precise diff-sized edits the user can eyeball, instead of rewriting the file).

    path: Absolute or ~ -relative path to the file.
    old_text: Exact existing text to replace (must match exactly once unless replace_all).
    new_text: Replacement text.
    replace_all: Replace every occurrence instead of requiring a unique match.
    """
    p = _safe_path(path)
    if not p.is_file():
        return {"error": f"File not found: {p}"}
    if not old_text:
        return {"error": "old_text is empty — for new content use write_file"}
    if old_text == new_text:
        return {"error": "old_text and new_text are identical"}
    content = p.read_text(errors="replace")
    n = content.count(old_text)
    if n == 0:
        return {"error": "old_text not found in file — it must match exactly"}
    if n > 1 and not replace_all:
        return {"error": f"old_text matches {n} times — make it unique or set replace_all"}
    p.write_text(content.replace(old_text, new_text))
    return {"status": "ok", "path": str(p), "replacements": n if replace_all else 1}


@skill(
    name="list_directory",
    description="List the files and folders in a directory — 'what files are in X', 'list my "
    "home folder'. Use this, not run_command, to list a directory.",
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
