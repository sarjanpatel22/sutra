"""DNS zone transfer (AXFR) check - a misconfigured nameserver hands over the whole zone.

dns_zone_transfer finds a domain's nameservers and asks each for a full AXFR. A server allowing it
dumps every record (all subdomains/hosts) - info disclosure and a big recon boost.
Read-only DNS queries via dig; degrades gracefully if dig is absent. CI-safe, $0.
"""

from __future__ import annotations

import shutil
from typing import Any

from friday.skills.recon import _run  # shared async subprocess helper (returncode, stdout, stderr)
from friday.skills.registry import skill


async def _nameservers(domain: str) -> list[str]:
    dig = shutil.which("dig")
    if not dig:
        return []
    _, out, _ = await _run([dig, "+short", "NS", domain], timeout=10)
    return [ln.strip().rstrip(".") for ln in (out or "").splitlines() if ln.strip()]


async def _axfr(domain: str, ns: str) -> tuple[bool, int]:
    """(transferred, record_count) for an AXFR of `domain` against nameserver `ns`."""
    dig = shutil.which("dig")
    if not dig:
        return False, 0
    _, out, _ = await _run([dig, f"@{ns}", domain, "AXFR", "+noall", "+answer"], timeout=20)
    text = out or ""
    if any(s in text for s in ("Transfer failed", "connection timed out", "communications error")):
        return False, 0
    records = [ln for ln in text.splitlines() if "\tIN\t" in ln or " IN " in ln]
    return (len(records) >= 2), len(records)  # >=2 real records means the zone actually came back


@skill(
    name="dns_zone_transfer",
    description=(
        "Check whether a domain's nameservers allow a DNS zone transfer (AXFR). Finds the NS "
        "records, then asks each nameserver for the full zone; a server that hands it over leaks "
        "every record (all subdomains/hosts) - information disclosure and a major recon boost. "
        "Read-only DNS queries (dig). Verify the dumped records are in scope before reporting."
    ),
    auto_confirm=True,  # read-only DNS recon (dig), like dns_lookup/whois
    timeout_s=60,
)
async def dns_zone_transfer(domain: str) -> dict[str, Any]:
    """Attempt an AXFR against each of the domain's nameservers.

    domain: the domain to check (a URL or host is accepted; the hostname is used).
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return {"error": "no domain to check"}
    if "://" in domain:
        from urllib.parse import urlparse
        domain = urlparse(domain).hostname or domain
    domain = domain.split("/")[0].split(":")[0]
    if not shutil.which("dig"):
        return {"error": "'dig' is not installed", "domain": domain}

    nameservers = await _nameservers(domain)
    if not nameservers:
        return {"domain": domain, "nameservers": [], "findings": [], "vulnerable": False,
                "note": "No NS records resolved - can't attempt AXFR."}

    results: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for ns in nameservers:
        ok, count = await _axfr(domain, ns)
        results.append({"nameserver": ns, "transferred": ok, "records": count})
        if ok:
            findings.append({
                "id": "dns-zone-transfer", "severity": "medium", "matched_at": f"{domain} @ {ns}",
                "name": f"DNS zone transfer (AXFR) allowed by {ns}",
                "description": (f"Nameserver {ns} allowed a full AXFR of {domain} and returned "
                                f"{count} records - the entire zone (every subdomain/host) is "
                                "exposed. Info disclosure; pull the zone for the full asset map."),
            })

    return {
        "domain": domain,
        "nameservers": nameservers,
        "results": results,
        "findings": findings,
        "vulnerable": bool(findings),
        "note": ("Zone transfer is medium (info disclosure). Use the dumped records to expand "
                 "the in-scope attack surface; confirm scope before reporting."),
    }
