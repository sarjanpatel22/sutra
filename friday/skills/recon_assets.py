"""Unified asset recon — one call that fans out across every subdomain source and merges the result.

recon_assets runs the passive subdomain sources in parallel — find_subdomains (subfinder) and
crtsh_subdomains (Certificate Transparency) — and merges them into one deduped, sorted asset list,
noting which source each came from and where they overlap. One source always misses what the other
catches (CT logs see issued-cert names; subfinder aggregates passive DNS), so the union is a much
wider attack surface than either alone — the assets you then probe with sutra hunt. Passive
(aggregates passive sources, never touches the target) → auto_confirm. Pure-stdlib, CI-safe, $0.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

from friday.skills.registry import skill


def _bare(domain: str) -> str:
    domain = (domain or "").strip()
    if "://" in domain:
        domain = urlparse(domain).hostname or domain
    return domain.split("/")[0].split(":")[0].lower().lstrip("*.")


@skill(
    name="recon_assets",
    description=(
        "Unified passive subdomain recon: fans out across find_subdomains (subfinder) and "
        "crtsh_subdomains (Certificate Transparency) in parallel and merges them into ONE deduped "
        "asset list, noting each source and the overlap. The union is a far wider attack surface "
        "than either source alone — the assets to then probe with sutra hunt. Passive (aggregates "
        "passive sources, never touches the target)."
    ),
    auto_confirm=True,  # aggregates passive OSINT sources, not the target
    timeout_s=90,
)
async def recon_assets(domain: str) -> dict[str, Any]:
    """Merge every passive subdomain source into one asset list.

    domain: the apex domain to enumerate (e.g. example.com).
    """
    from friday.skills.crtsh import crtsh_subdomains
    from friday.skills.recon import find_subdomains

    bare = _bare(domain)
    if not bare:
        return {"error": "no domain to look up"}

    sub_res, crt_res = await asyncio.gather(
        find_subdomains(bare), crtsh_subdomains(bare), return_exceptions=True
    )

    sources: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    for name, res in (("subfinder", sub_res), ("crtsh", crt_res)):
        if isinstance(res, Exception):
            errors[name] = str(res)[:120]
            continue
        if res.get("error"):
            errors[name] = res["error"]
        sources[name] = res.get("subdomains", [])

    merged = sorted({s for names in sources.values() for s in names})
    only_crt = sorted(set(sources.get("crtsh", [])) - set(sources.get("subfinder", [])))

    note = (
        f"{len(merged)} unique assets from {len(sources)} source(s); "
        f"{len(only_crt)} found ONLY via Certificate Transparency (subfinder missed them). "
        "Probe the live, in-scope ones with `sutra hunt`."
    )
    if errors:
        note += f" Source issues: {errors} — coverage may be partial, not 'no assets'."

    return {
        "domain": bare,
        "total": len(merged),
        "subdomains": merged[:2000],
        "by_source": {k: len(v) for k, v in sources.items()},
        "crt_only": only_crt[:200],
        "errors": errors,
        "note": note,
    }
