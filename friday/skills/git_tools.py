"""Git skills — status/diff auto-confirmed reads, commit gated.

The IDE-bridge slot of Phase 1.6 starts here: Sutra can see a repo's state and
history-to-be, and can commit — but a commit rewrites history forward, so it is
auto_confirm=False like every other mutation. All commands run argv-style (no
shell) against an explicit repo path confined to $HOME.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from friday.skills.file_ops import _safe_path
from friday.skills.registry import skill

_MAX_OUT = 12000  # cap diff/log output to keep model context sane


def _repo_path(repo: str) -> Path:
    """Resolve repo within $HOME and require it to actually be a git repo."""
    p = _safe_path(repo)
    if not (p / ".git").exists():
        raise ValueError(f"not a git repository: {p}")
    return p


async def _git(repo: Path, *args: str) -> tuple[int, str, str]:
    """Run one git command argv-style in repo; (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return (
        proc.returncode or 0,
        out.decode(errors="replace")[:_MAX_OUT],
        err.decode(errors="replace")[:2000],
    )


@skill(
    name="git_status",
    description="Show a git repo's branch and changed/untracked files.",
    auto_confirm=True,
    timeout_s=10,
)
async def git_status(repo: str = "~/friday-os") -> dict[str, Any]:
    """Current branch + working-tree state of a repository.

    repo: Path to the git repository (default ~/friday-os).
    """
    try:
        p = _repo_path(repo)
    except (ValueError, PermissionError) as e:
        return {"error": str(e)}
    code, out, err = await _git(p, "status", "--porcelain=v1", "--branch")
    if code != 0:
        return {"error": err.strip() or f"git status failed ({code})"}
    lines = out.splitlines()
    branch = lines[0].removeprefix("## ") if lines else ""
    changes = lines[1:]
    return {"repo": str(p), "branch": branch, "changes": changes, "clean": not changes}


@skill(
    name="git_diff",
    description=(
        "Show a git diff: working tree by default, staged changes, or any ref/range "
        "(e.g. 'main...HEAD')."
    ),
    auto_confirm=True,
    timeout_s=15,
)
async def git_diff(
    repo: str = "~/friday-os", ref: str = "", staged: bool = False, path: str = ""
) -> dict[str, Any]:
    """Diff of uncommitted, staged, or committed changes.

    repo: Path to the git repository (default ~/friday-os).
    ref: Optional ref or range to diff (e.g. "HEAD~1" or "main...HEAD").
    staged: Diff the staging area instead of the working tree.
    path: Optional pathspec to limit the diff.
    """
    try:
        p = _repo_path(repo)
    except (ValueError, PermissionError) as e:
        return {"error": str(e)}
    args = ["diff"]
    if staged:
        args.append("--staged")
    if ref:
        # --end-of-options stops git parsing a ref that begins with '-' as an option
        # (e.g. a crafted ref="--output=FILE" would otherwise make `git diff` write to an
        # arbitrary file — an unintended write from this auto_confirm read). Refs/ranges
        # like HEAD~1 or main...HEAD are unaffected.
        args += ["--end-of-options", ref]
    if path:
        args += ["--", path]
    code, out, err = await _git(p, *args)
    if code != 0:
        return {"error": err.strip() or f"git diff failed ({code})"}
    return {"repo": str(p), "diff": out, "empty": not out.strip(),
            "truncated": len(out) >= _MAX_OUT}


@skill(
    name="git_commit",
    description=(
        "Stage the given paths (or all changes) and create a git commit. "
        "IRREVERSIBLE (writes history) — requires confirmation."
    ),
    auto_confirm=False,
    timeout_s=20,
)
async def git_commit(
    message: str,
    repo: str = "~/friday-os",
    paths: list[str] | None = None,
    all_changes: bool = False,
) -> dict[str, Any]:
    """Stage and commit changes.

    message: The commit message.
    repo: Path to the git repository (default ~/friday-os).
    paths: Specific paths to stage; required unless all_changes is true.
    all_changes: Stage every change in the repo (git add -A).
    """
    message = message.strip()
    if not message:
        return {"error": "empty commit message"}
    if not all_changes and not paths:
        return {"error": "give paths to stage, or set all_changes=true"}
    try:
        p = _repo_path(repo)
    except (ValueError, PermissionError) as e:
        return {"error": str(e)}

    add_args = ["add", "-A"] if all_changes else ["add", "--", *[str(x) for x in paths or []]]
    code, _, err = await _git(p, *add_args)
    if code != 0:
        return {"error": f"git add failed: {err.strip()}"}

    code, out, err = await _git(p, "commit", "-m", message)
    if code != 0:
        return {"error": err.strip() or out.strip() or f"git commit failed ({code})"}
    code, sha, _ = await _git(p, "rev-parse", "--short", "HEAD")
    return {"repo": str(p), "commit": sha.strip(), "summary": out.strip()[:500]}
