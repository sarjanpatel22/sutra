"""Subdomain-takeover detection — a dangling DNS record pointing at an unclaimed service.

check_takeover resolves a host's CNAME and fetches it over HTTP, then matches both against the known
can-i-take-over-xyz fingerprints (GitHub Pages, S3, Heroku, Fastly, Azure, Surge, …). The payable
signal: the host serves the provider's unclaimed-resource page (NoSuchBucket / "There isn't a
GitHub Pages site here" / "No such app") — claim the resource and you own the subdomain. PASSIVE:
one DNS lookup (dig, optional) + one GET. Pure-stdlib HTTP; degrades gracefully if dig is absent.
Confirm by actually claiming the resource (in scope) before reporting. CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import shutil
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from friday.skills.recon import _run  # shared async subprocess helper (returncode, stdout, stderr)
from friday.skills.registry import skill

# (service, CNAME substring, body fingerprint of the UNCLAIMED page, severity). From the community
# can-i-take-over-xyz matrix — the high-signal, still-exploitable ones.
_FINGERPRINTS: list[tuple[str, str, str, str]] = [
    ("GitHub Pages", "github.io", "There isn't a GitHub Pages site here", "high"),
    ("AWS S3", "s3.amazonaws.com", "NoSuchBucket", "high"),
    ("AWS S3", "amazonaws.com", "The specified bucket does not exist", "high"),
    ("Heroku", "herokuapp.com", "No such app", "high"),
    ("Heroku", "herokudns.com", "No such app", "high"),
    ("Fastly", "fastly.net", "Fastly error: unknown domain", "high"),
    ("Surge.sh", "surge.sh", "project not found", "high"),
    ("Bitbucket", "bitbucket.io", "Repository not found", "high"),
    ("Pantheon", "pantheonsite.io", "404 error unknown site", "high"),
    ("Azure", "azurewebsites.net", "404 Web Site not found", "high"),
    ("Shopify", "myshopify.com", "Sorry, this shop is currently unavailable", "medium"),
    ("Ghost", "ghost.io", "Domain error", "medium"),
    (
        "Tumblr",
        "domains.tumblr.com",
        "Whatever you were looking for doesn't currently exist",
        "medium",
    ),
    ("Webflow", "proxy-ssl.webflow.com", "The page you are looking for doesn't exist", "medium"),
    ("Readme.io", "readme.io", "Project doesnt exist", "medium"),
    ("Unbounce", "unbouncepages.com", "The requested URL was not found on this server", "low"),
]


async def _cname(host: str) -> str:
    """Best-effort CNAME of `host` via dig (+short). Empty if dig is absent or there's no CNAME."""
    dig = shutil.which("dig")
    if not dig:
        return ""
    rc, out, _ = await _run([dig, "+short", "CNAME", host], timeout=10)
    lines = [ln.strip().rstrip(".").lower() for ln in (out or "").splitlines() if ln.strip()]
    return lines[0] if lines else ""


def _fetch_body(host: str) -> str:
    """Body of the host over https then http (best-effort). Empty on failure. A 4xx/5xx page is
    what we want (it's the provider's unclaimed-resource page), so its body is returned too."""
    for scheme in ("https", "http"):
        req = urllib.request.Request(
            f"{scheme}://{host}", headers={"User-Agent": "sutra-takeover/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 — authorized target
                body: str = r.read(500_000).decode("utf-8", "replace")
                return body
        except urllib.error.HTTPError as e:
            try:
                return e.read(500_000).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return ""
        except (urllib.error.URLError, OSError):
            continue
    return ""


@skill(
    name="check_takeover",
    description=(
        "Check a host for subdomain takeover — a dangling DNS record pointing at an unclaimed "
        "service (GitHub Pages, S3, Heroku, Fastly, Azure, Surge, …). Resolves the CNAME "
        "and fetches the host, then matches both against the known provider 'unclaimed resource' "
        "fingerprints (NoSuchBucket, 'There isn't a GitHub Pages site here', 'No such app', …). A "
        "match means you can likely claim the resource and control the subdomain — a clean payable "
        "find. Passive (one DNS lookup + one GET). Confirm by claiming the resource (in scope)."
    ),
    auto_confirm=True,  # one DNS lookup + one GET — passive recon
    timeout_s=40,
)
async def check_takeover(host: str) -> dict[str, Any]:
    """Resolve a host's CNAME + body and flag a likely subdomain takeover.

    host: the subdomain to check (a URL is accepted; the hostname is used).
    """
    host = (host or "").strip()
    if not host:
        return {"error": "no host to check"}
    if "://" in host:
        host = urlparse(host).hostname or host
    host = host.split("/")[0].split(":")[0].lower()

    cname = await _cname(host)
    body = await asyncio.to_thread(_fetch_body, host)

    findings: list[dict[str, Any]] = []
    candidate: tuple[str, str] | None = (
        None  # (service, cname) — points to a service but not unclaimed
    )
    for service, cpat, fp, sev in _FINGERPRINTS:
        if body and fp.lower() in body.lower():
            also = (
                f"; its CNAME points to {service} ({cname})"
                if cname and cpat in cname
                else " (CNAME not resolved here — confirm it's a dangling record)"
            )
            findings.append(
                {
                    "id": "subdomain-takeover",
                    "severity": sev,
                    "matched_at": host,
                    "name": f"Possible subdomain takeover — {service}",
                    "description": (
                        f"{host} serves {service}'s unclaimed-resource page (matched "
                        f"'{fp}'){also}. Claim the resource (in scope) to confirm takeover."
                    ),
                }
            )
            break  # the served fingerprint is the definitive signal — one service per host
        if cname and cpat in cname and candidate is None:
            candidate = (service, cname)
    if not findings and candidate is not None:
        service, cn = candidate
        findings.append(
            {
                "id": "subdomain-takeover-candidate",
                "severity": "info",
                "matched_at": host,
                "name": f"CNAME points to {service} — verify it is claimed",
                "description": (
                    f"{host} CNAMEs to {service} ({cn}). If that resource is unclaimed the "
                    "subdomain is takeover-able; the host didn't serve an obvious unclaimed "
                    "page here, so verify the resource's status directly."
                ),
            }
        )

    note = (
        "Confirm by registering/claiming the dangling resource (in scope) — a live, claimed "
        "service is not a finding."
    )
    if not cname and not body:
        note = "Inconclusive — couldn't resolve a CNAME (is dig installed?) or fetch the host."
    return {
        "host": host,
        "cname": cname,
        "served_body": bool(body),
        "findings": findings,
        "vulnerable": any(f["id"] == "subdomain-takeover" for f in findings),
        "note": note,
    }
