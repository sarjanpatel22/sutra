"""GitHub secret recon — finding a target's leaked credentials in public GitHub code.

scan_github runs GitHub code-search (via the authenticated gh CLI) for a target keyword paired with
secret indicators, then CONFIRMS each hit by regex-matching the returned code fragment against real
secret formats (AWS/Google/GitHub/Slack/Stripe keys, private-key blocks). Developers leak API keys,
tokens and credentials in public repos constantly — a steady, very findable bug-bounty payout that
no on-target scanner can see. Confirmed-only: a result is reported only when a fragment actually
matches a secret pattern, not merely mentions the keyword. Passive (queries GitHub, never the
target). Needs the gh CLI authenticated; degrades gracefully if absent. $0.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from typing import Any

from friday.skills.recon import _run  # shared async subprocess helper (returncode, stdout, stderr)
from friday.skills.registry import skill

# (label, pattern) — a fragment matching one of these is a confirmed leaked secret.
_SECRET_RE: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("AWS secret key", re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+]{40}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("GitHub token", re.compile(r"gh[pousr]_[0-9A-Za-z]{36,}|github_pat_[0-9A-Za-z_]{22,}")),
    ("Slack token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}")),
    ("Stripe key", re.compile(r"sk_live_[0-9A-Za-z]{24,}")),
    ("Private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("Generic credential",
     re.compile(r"(?i)(?:api[_-]?key|secret|passwd|password|access[_-]?token)"
                r"['\"]?\s*[:=]\s*['\"][0-9A-Za-z\-_/+!@#$%^&*.]{12,}['\"]")),
]


def _detect(fragment: str) -> tuple[str, str] | None:
    for label, pat in _SECRET_RE:
        m = pat.search(fragment or "")
        if m:
            return label, m.group(0)[:60]
    return None


async def _gh_search(query: str, per_page: int = 20) -> tuple[list[dict[str, Any]], str | None]:
    """Run a GitHub code search via the gh CLI, returning (items, error)."""
    gh = shutil.which("gh")
    if not gh:
        return [], "gh CLI not installed — install + authenticate it to search GitHub"
    cmd = [gh, "api", "-H", "Accept: application/vnd.github.text-match+json",
           "--method", "GET", "search/code", "-f", f"q={query}", "-F", f"per_page={per_page}"]
    rc, out, err = await _run(cmd, timeout=30)
    if rc != 0:
        return [], (err or "gh api failed (auth/rate-limit?)").strip()[:160]
    try:
        return json.loads(out).get("items", []), None
    except json.JSONDecodeError:
        return [], "unparseable gh response"


@skill(
    name="scan_github",
    description=(
        "Search public GitHub for a target's leaked secrets (via the authenticated gh CLI). Runs "
        "code search for the target keyword paired with secret indicators, then confirms each hit "
        "by regex-matching the returned code fragment against real secret formats (AWS/Google/"
        "GitHub/Slack/Stripe keys, private keys). Reports only fragments that actually contain a "
        "secret — a steady, scanner-blind bug-bounty payout. Pass an org name, company or domain "
        "keyword. Passive (queries GitHub, never the target); needs gh installed + authenticated."
    ),
    auto_confirm=True,  # passive OSINT against GitHub, not the target
    timeout_s=120,
)
async def scan_github(target: str, max_results: int = 30) -> dict[str, Any]:
    """Hunt a target's leaked secrets across public GitHub code.

    target: an org name, company, or domain keyword to search for (e.g. "acme" or "acme.com").
    max_results: cap on confirmed leaks to return.
    """
    target = (target or "").strip()
    if not target:
        return {"error": "no target keyword to search"}

    queries = [
        f'"{target}" AKIA',
        f'"{target}" "api_key"',
        f'"{target}" "BEGIN RSA PRIVATE KEY"',
        f'"{target}" password',
    ]

    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    last_err: str | None = None
    for q in queries:
        items, err = await _gh_search(q)
        if err is not None:
            last_err = err
            continue
        for it in items:
            repo = (it.get("repository") or {}).get("full_name", "?")
            path = it.get("path", "?")
            html = it.get("html_url", "")
            for tm in it.get("text_matches", []):
                hit = _detect(tm.get("fragment", ""))
                if not hit:
                    continue
                label, snippet = hit
                key = (repo, path, label)
                if key in seen:
                    continue
                seen.add(key)
                findings.append({
                    "id": "github-leaked-secret",
                    "severity": "high",
                    "matched_at": html or f"{repo}/{path}",
                    "name": f"{label} leaked in {repo}",
                    "description": (
                        f"A {label} appears in public repo {repo} at {path} (matched "
                        f"'{snippet}'). Verify it is live and belongs to the target, then report — "
                        "leaked credentials are a direct, high-severity finding."
                    ),
                })
        await asyncio.sleep(0)  # cooperative; GitHub code search is rate-limited

    if not findings and last_err and len(seen) == 0:
        return {"error": f"github search unavailable: {last_err}", "target": target}

    return {
        "target": target,
        "queries": queries,
        "findings": findings[:max_results],
        "count": len(findings),
        "note": (
            "Confirmed by a secret-pattern match in the code, not just a keyword mention. Verify "
            "each secret is live and in scope before reporting; rotate-on-disclosure is the "
            "expected remediation. Some valid leaks need broader dorks — refine the keyword if "
            "results seem thin."
        ),
    }
