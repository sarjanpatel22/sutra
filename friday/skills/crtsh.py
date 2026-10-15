"""Certificate-transparency subdomain discovery (crt.sh + certspotter, retried).

crtsh_subdomains queries public Certificate Transparency sources for every hostname that has ever
appeared in a TLS certificate for a domain. CT logs are a goldmine of passive recon: they reveal
subdomains (staging, internal, dev, vpn, admin) that DNS brute-force and subfinder can miss. crt.sh
is the primary source but is frequently slow / 502s, so this retries it with backoff AND falls back
to certspotter — a crt.sh outage no longer means zero results, and the union widens coverage. Use
alongside find_subdomains. PASSIVE: queries CT services, never the target. Pure-stdlib, CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, urlparse

from friday.skills.registry import skill

_CRTSH = "https://crt.sh/"  # overridable in tests
_CERTSPOTTER = "https://api.certspotter.com/v1/issuances"  # overridable in tests
_RETRY_CODES = {429, 500, 502, 503, 504}  # transient — worth retrying


def _bare(domain: str) -> str:
    domain = (domain or "").strip()
    if "://" in domain:
        domain = urlparse(domain).hostname or domain
    return domain.split("/")[0].split(":")[0].lower().lstrip("*.")


def _fetch(url: str, attempts: int = 3) -> tuple[str, str | None]:
    """GET with retry+backoff on transient failures (5xx/429/timeout/refused)."""
    last = "unknown error"
    for i in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": "sutra-crtsh/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:  # noqa: S310 — CT service, not target
                return r.read(12_000_000).decode("utf-8", "replace"), None
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in _RETRY_CODES:
                return "", last
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            last = str(e)[:120]
        if i < attempts - 1:
            time.sleep(0.4 * (i + 1))  # brief backoff before retry
    return "", last


def _in_scope(name: str, bare: str) -> str | None:
    name = name.strip().lower().lstrip("*.")
    if name and (name == bare or name.endswith("." + bare)) and "@" not in name:
        return name
    return None


def _parse_crtsh(body: str, bare: str) -> set[str]:
    try:
        rows = json.loads(body) if body.strip() else []
    except json.JSONDecodeError:
        return set()
    names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in ("name_value", "common_name"):
            for raw in str(row.get(field, "")).splitlines():
                n = _in_scope(raw, bare)
                if n:
                    names.add(n)
    return names


def _parse_certspotter(body: str, bare: str) -> set[str]:
    try:
        rows = json.loads(body) if body.strip() else []
    except json.JSONDecodeError:
        return set()
    names: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        for raw in (row.get("dns_names", []) if isinstance(row, dict) else []):
            n = _in_scope(str(raw), bare)
            if n:
                names.add(n)
    return names


@skill(
    name="crtsh_subdomains",
    description=(
        "Discover subdomains from Certificate Transparency logs (crt.sh + certspotter, with "
        "retries). Returns every hostname that has appeared in a TLS certificate for the domain — "
        "staging/dev/internal/vpn/admin names that DNS brute-force and subfinder often miss. "
        "Retries crt.sh on its frequent 502s and falls back to certspotter, so an outage doesn't "
        "mean zero results. Passive (queries CT services, never the target); pair with "
        "find_subdomains, then probe the live ones with sutra hunt."
    ),
    auto_confirm=True,  # passive OSINT against CT services, not the target
    timeout_s=90,
)
async def crtsh_subdomains(domain: str) -> dict[str, Any]:
    """Pull a domain's subdomains from Certificate Transparency (crt.sh + certspotter).

    domain: the apex domain to enumerate (e.g. example.com).
    """
    bare = _bare(domain)
    if not bare:
        return {"error": "no domain to look up"}

    def _run() -> tuple[set[str], list[str], dict[str, str]]:
        names: set[str] = set()
        ok: list[str] = []
        errors: dict[str, str] = {}
        # source 1: crt.sh
        body, err = _fetch(f"{_CRTSH}?q={quote('%.' + bare)}&output=json")
        if err is not None:
            errors["crt.sh"] = err
        else:
            names |= _parse_crtsh(body, bare)
            ok.append("crt.sh")
        # source 2: certspotter (fallback + extra coverage)
        body, err = _fetch(
            f"{_CERTSPOTTER}?domain={quote(bare)}&include_subdomains=true&expand=dns_names")
        if err is not None:
            errors["certspotter"] = err
        else:
            names |= _parse_certspotter(body, bare)
            ok.append("certspotter")
        return names, ok, errors

    names, ok, errors = await asyncio.to_thread(_run)
    if not ok:  # every CT source failed
        return {"error": f"all CT sources failed: {errors}", "domain": bare}

    subs = sorted(names)
    note = (
        f"{len(subs)} unique name(s) from {', '.join(ok)}. Merge with find_subdomains, then probe "
        "the live ones (sutra hunt). CT can list dead/internal names — confirm each resolves and "
        "is in scope before touching it."
    )
    if errors:
        note += f" (source issues: {errors} — coverage may be partial)"

    return {
        "domain": bare,
        "count": len(subs),
        "subdomains": subs[:1000],
        "sources": ok,
        "errors": errors,
        "note": note,
    }
