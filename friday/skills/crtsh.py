"""Certificate-transparency subdomain discovery via crt.sh.

crtsh_subdomains queries the public crt.sh certificate-transparency database for every hostname that
has ever appeared in a TLS certificate for a domain. CT logs are a goldmine of passive recon: they
reveal subdomains (staging, internal, dev, vpn, admin) that DNS brute-force and subfinder can miss,
because anyone who issued a cert for them is on record. Use alongside find_subdomains for wider
asset coverage. PASSIVE: queries crt.sh, never the target. Pure-stdlib (urllib + json),
CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, urlparse

from friday.skills.registry import skill

_CRTSH = "https://crt.sh/"  # overridable in tests


def _bare(domain: str) -> str:
    domain = (domain or "").strip()
    if "://" in domain:
        domain = urlparse(domain).hostname or domain
    return domain.split("/")[0].split(":")[0].lower().lstrip("*.")


def _fetch(url: str) -> tuple[str, str | None]:
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-crtsh/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 — crt.sh, not the target
            return r.read(12_000_000).decode("utf-8", "replace"), None
    except (urllib.error.URLError, OSError) as e:
        return "", str(e)[:160]


@skill(
    name="crtsh_subdomains",
    description=(
        "Discover subdomains from Certificate Transparency logs via crt.sh. Returns every hostname "
        "that has appeared in a TLS certificate for the domain — staging/dev/internal/vpn/admin "
        "names that DNS brute-force and subfinder often miss. Passive recon (queries crt.sh, never "
        "the target); use alongside find_subdomains for wider asset coverage, then probe the live "
        "ones with sutra hunt."
    ),
    auto_confirm=True,  # passive OSINT against crt.sh, not the target
    timeout_s=45,
)
async def crtsh_subdomains(domain: str) -> dict[str, Any]:
    """Pull a domain's subdomains from Certificate Transparency (crt.sh).

    domain: the apex domain to enumerate (e.g. example.com).
    """
    bare = _bare(domain)
    if not bare:
        return {"error": "no domain to look up"}

    body, err = await asyncio.to_thread(_fetch, f"{_CRTSH}?q={quote('%.' + bare)}&output=json")
    if err is not None:
        return {"error": f"crt.sh lookup failed: {err}", "domain": bare}
    try:
        rows = json.loads(body) if body.strip() else []
    except json.JSONDecodeError:
        return {"error": "crt.sh returned unparseable data", "domain": bare}

    names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        # name_value can hold several newline-separated names (incl. wildcards)
        for field in ("name_value", "common_name"):
            for raw in str(row.get(field, "")).splitlines():
                name = raw.strip().lower().lstrip("*.")
                if name and (name == bare or name.endswith("." + bare)) and "@" not in name:
                    names.add(name)

    subs = sorted(names)
    return {
        "domain": bare,
        "count": len(subs),
        "subdomains": subs[:1000],
        "note": (
            f"{len(subs)} unique name(s) from Certificate Transparency. Merge with "
            "find_subdomains, then probe the live ones (sutra hunt). CT can list dead/internal "
            "names — confirm each resolves and is in scope before touching it."
        ),
    }
