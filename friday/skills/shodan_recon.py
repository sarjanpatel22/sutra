"""Shodan host lookup — passive OSINT on a target's internet-facing footprint, from Shodan's
existing scan data. NO traffic to the target: Shodan already scanned it, we just read the record.

Surfaces open ports, running products+versions, hostnames, org/ISP, and (the bounty gold) known
CVEs Shodan has tagged for that host — the fastest "what's exposed and what's it running" before you
ever touch the box. Key-gated: needs a Shodan API key (free tier works) via keyring/`SHODAN_API_KEY`
and degrades to a clear instruction if absent (CI-safe). Passive OSINT, auto-confirmed. Pure, $0.
"""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
from typing import Any

from friday.core.secrets import get_secret
from friday.skills.registry import skill

_API = "https://api.shodan.io/shodan/host/{ip}?key={key}"


def _resolve(target: str) -> tuple[str | None, str | None]:
    """Reduce a URL/host to a bare host, then resolve to an IP. Returns (ip, error)."""
    host = target.strip().lower().split("://")[-1].split("/")[0].split(":")[0]
    if not host:
        return None, "no target"
    # already an IP? socket.gethostbyname returns it unchanged
    try:
        return socket.gethostbyname(host), None
    except (socket.gaierror, OSError) as e:
        return None, f"could not resolve {host}: {str(e)[:80]}"


def _lookup(ip: str, key: str) -> dict[str, Any]:
    """GET the Shodan host record. Maps HTTP errors to friendly results."""
    url = _API.format(ip=ip, key=key)
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-shodan/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 — fixed Shodan API host
            record: dict[str, Any] = json.loads(r.read(2_000_000).decode("utf-8", "replace"))
            return record
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"_none": True}  # Shodan has no record for this host
        if e.code == 401:
            return {"_error": "Shodan rejected the API key (401) — check SHODAN_API_KEY"}
        return {"_error": f"Shodan HTTP {e.code}"}
    except (urllib.error.URLError, OSError) as e:
        return {"_error": f"could not reach Shodan: {str(e)[:80]}"}
    except json.JSONDecodeError:
        return {"_error": "Shodan returned non-JSON"}


@skill(
    name="shodan_host",
    description=(
        "Look up a host on Shodan — PASSIVE OSINT (reads Shodan's existing scan data, sends NO "
        "traffic to the target). Returns open ports, running products+versions, hostnames, org/ISP "
        "and known CVEs Shodan has tagged for the host. The fastest 'what's exposed and what's it "
        "running' before touching the box. Needs a Shodan API key (keyring or SHODAN_API_KEY). "
        "Accepts an IP or a domain (resolved first). Authorized targets only."
    ),
    auto_confirm=True,  # passive OSINT — reads Shodan, never contacts the target
    timeout_s=30,
)
async def shodan_host(target: str) -> dict[str, Any]:
    """Fetch a host's Shodan record (open ports, services, CVEs).

    target: an IP or domain. A domain/URL is reduced to a host and resolved to an IP first.
    """
    target = (target or "").strip()
    if not target:
        return {"error": "no target — give an IP or domain"}
    key = get_secret("SHODAN_API_KEY")
    if not key:
        return {"error": "no Shodan API key — set SHODAN_API_KEY (keyring or env); free tier works"}

    ip, err = await asyncio.to_thread(_resolve, target)
    if err or ip is None:
        return {"error": err or f"could not resolve {target}", "target": target}
    data = await asyncio.to_thread(_lookup, ip, key)
    if data.get("_error"):
        return {"error": data["_error"], "ip": ip}
    if data.get("_none"):
        return {"ip": ip, "target": target, "count": 0,
                "note": "no Shodan record for this host (not scanned, or nothing exposed)"}

    services = [
        {
            "port": s.get("port"),
            "transport": s.get("transport"),
            "product": s.get("product"),
            "version": s.get("version"),
            "banner": (s.get("data") or "").strip()[:160] or None,
        }
        for s in data.get("data", [])
    ]
    vulns = sorted(data.get("vulns", []) or [])
    out_ports = sorted(data.get("ports", []) or [])
    return {
        "ip": ip,
        "target": target,
        "hostnames": data.get("hostnames", []),
        "org": data.get("org"),
        "isp": data.get("isp"),
        "os": data.get("os"),
        "ports": out_ports,
        "vulns": vulns,
        "services": services,
        "count": len(services),
        "note": (
            f"{len(out_ports)} open port(s), {len(vulns)} CVE(s) tagged by Shodan. "
            "These are Shodan's cached observations — re-verify each is live (port_scan / nuclei) "
            "before reporting; a tagged CVE is a lead, not a confirmed finding."
        ),
    }
