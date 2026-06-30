"""Wayback URL harvesting — pulling a domain's historical URLs from the Internet Archive.

wayback_urls queries the Wayback Machine CDX API for every URL the archive has ever seen under a
domain. This is prime bug-bounty recon: it surfaces forgotten endpoints, old API paths, and — most
valuably — parameterised URLs that aren't linked anywhere anymore but often still work, feeding the
injection skills (check_sqli / test_reflected_xss / check_open_redirect) real targets to test.
PASSIVE: it queries archive.org, never the target itself (so auto_confirm, like find_subdomains).
Pure-stdlib (urllib + json), CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, urlparse

from friday.skills.registry import skill

_CDX = "http://web.archive.org/cdx/search/cdx"  # overridable in tests


def _bare(domain: str) -> str:
    domain = (domain or "").strip()
    if "://" in domain:
        domain = urlparse(domain).hostname or domain
    return domain.split("/")[0].split(":")[0].lower().lstrip("*.")


def _fetch(url: str) -> tuple[str, str | None]:
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-wayback/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 — archive.org, not target
            return r.read(8_000_000).decode("utf-8", "replace"), None
    except (urllib.error.URLError, OSError) as e:
        return "", str(e)[:160]


@skill(
    name="wayback_urls",
    description=(
        "Harvest a domain's historical URLs from the Wayback Machine (Internet Archive CDX API). "
        "Prime bug-bounty recon: surfaces forgotten endpoints, old API paths, and parameterised "
        "URLs that aren't linked anymore but often still work — feed them to the injection skills "
        "(check_sqli / test_reflected_xss / check_open_redirect / find_params). params_only=True "
        "keeps only URLs with query parameters (the injectable ones). Passive — queries "
        "archive.org, never the target."
    ),
    auto_confirm=True,  # passive OSINT against archive.org, not the target
    timeout_s=45,
)
async def wayback_urls(domain: str, limit: int = 2000, params_only: bool = False) -> dict[str, Any]:
    """Pull a domain's archived URLs from the Wayback Machine.

    domain: the target domain (subdomains are included via the /* match).
    limit: max URLs to request from the archive.
    params_only: keep only URLs that carry query parameters (the injectable surface).
    """
    bare = _bare(domain)
    if not bare:
        return {"error": "no domain to look up"}

    url = (f"{_CDX}?url={quote(bare)}/*&output=json&fl=original"
           f"&collapse=urlkey&limit={max(1, int(limit))}")
    body, err = await asyncio.to_thread(_fetch, url)
    if err is not None:
        return {"error": f"wayback lookup failed: {err}", "domain": bare}

    try:
        rows = json.loads(body) if body.strip() else []
    except json.JSONDecodeError:
        return {"error": "wayback returned unparseable data", "domain": bare}

    # CDX JSON is [[header], [url], [url], ...] — skip the header row.
    originals = [r[0] for r in rows[1:] if r] if rows and isinstance(rows[0], list) else []
    seen: dict[str, None] = {}
    for u in originals:
        seen.setdefault(u, None)
    urls = list(seen)
    with_params = [u for u in urls if "?" in u and "=" in u]
    if params_only:
        urls = with_params

    return {
        "domain": bare,
        "total": len(urls),
        "with_params": len(with_params),
        "urls": urls[:500],  # sample cap for the response; counts reflect the full set
        "note": (
            f"{len(with_params)} parameterised URL(s) found — the injectable surface. Feed them to "
            "check_sqli / test_reflected_xss / check_open_redirect, and the rest to find_params. "
            "Archived URLs may be stale; confirm each is still live before testing."
        ),
    }
