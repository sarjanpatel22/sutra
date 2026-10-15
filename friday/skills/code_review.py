"""Code review skill — read a git diff, return reviewer comments. Read-only.

Phase 1.6 IDE bridge: the reviewing model is the local code tier (qwen2.5:14b
on demand, free cloud fallback), so a review costs $0 and no code leaves the
machine on the happy path. Reviewing mutates nothing → auto_confirm=True.
"""

from __future__ import annotations

from typing import Any

from friday.core.router import Tier, complete
from friday.skills.git_tools import _git, _repo_path
from friday.skills.registry import skill

# qwen2.5:14b runs at Ollama's default context window, so keep diff + prompt +
# review comfortably inside it; bigger diffs get truncated with a note.
_MAX_DIFF = 9000

_REVIEW_PROMPT = """You are a dry, precise senior engineer reviewing a git diff. \
Report only findings that matter: bugs, broken edge cases, security issues, dead or \
duplicated code. Number each finding, cite the file (and hunk/line when possible), \
and tag it [bug], [risk], or [cleanup]. No style nits, no praise. If the diff is \
genuinely fine, reply exactly: LGTM — no findings."""


@skill(
    name="code_review",
    description=(
        "Review code changes in a git repo: reads the diff (working tree, staged, or "
        "a ref/range like 'main...HEAD') and returns numbered reviewer findings."
    ),
    auto_confirm=True,
    timeout_s=120,
)
async def code_review(
    repo: str = "~/friday-os", ref: str = "", staged: bool = False, focus: str = ""
) -> dict[str, Any]:
    """Review a diff and return findings.

    repo: Path to the git repository (default ~/friday-os).
    ref: Optional ref or range to review (e.g. "HEAD~1" or "main...HEAD").
    staged: Review the staging area instead of the working tree.
    focus: Optional aspect to weight (e.g. "error handling", "security").
    """
    try:
        p = _repo_path(repo)
    except (ValueError, PermissionError) as e:
        return {"error": str(e)}
    args = ["diff"]
    if staged:
        args.append("--staged")
    if ref:
        # --end-of-options: a ref beginning with '-' (e.g. "--output=FILE") must not be
        # parsed as a git option, or this read could write an arbitrary file. See git_diff.
        args += ["--end-of-options", ref]
    code, diff, err = await _git(p, *args)
    if code != 0:
        return {"error": err.strip() or f"git diff failed ({code})"}
    diff = diff.strip()
    if not diff:
        return {"review": "nothing to review — the requested diff is empty",
                "repo": str(p), "diff_chars": 0}

    truncated = len(diff) > _MAX_DIFF
    body = diff[:_MAX_DIFF]
    user = f"Review this diff{f', focusing on {focus}' if focus else ''}:\n\n{body}"
    if truncated:
        user += "\n\n[diff truncated — review what is shown]"
    messages = [
        {"role": "system", "content": _REVIEW_PROMPT},
        {"role": "user", "content": user},
    ]
    try:
        r = await complete(messages, tier=Tier.CODE, tools=None, max_tokens=700, think=False)
    except Exception as e:  # noqa: BLE001 — local model and free fallbacks all down
        return {"error": f"review model unavailable: {e}", "repo": str(p)}
    return {
        "review": (r.choices[0].message.content or "").strip(),
        "repo": str(p),
        "diff_chars": len(diff),
        "truncated": truncated,
    }
