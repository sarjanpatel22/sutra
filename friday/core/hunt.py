"""sutra hunt — the one-command bug-bounty program run.

Chains the pieces into a single flow over a whole program: find_subdomains (the assets) → probe
which are live → nuclei_scan each → ONE combined, submittable bounty report. The TOOLS find (real,
grounded output), the report assembles itself, YOU verify + submit. Authorized programs only.

A bare domain (example.com) is auto-enumerated; a URL/host (http://x, host:port) is scanned as the
single target. The live-host probe + the per-host scan cap keep a real program run bounded.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

ObserveFn = Callable[[str, dict[str, Any]], None] | None

# A bare apex/host (no scheme, port, or path) — the signal to enumerate subdomains.
_BARE_DOMAIN = re.compile(r"^(?!-)[a-z0-9-]+(?:\.[a-z0-9-]+)+$")


async def _is_live_web(host: str, timeout: float = 2.0) -> str | None:
    """The host's URL if it answers on 443 or 80 (a fast TCP-connect probe), else None."""
    for port, scheme in ((443, "https"), (80, "http")):
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        except (OSError, TimeoutError):
            continue
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return f"{scheme}://{host}"
    return None


async def _live_web_hosts(hosts: list[str], max_hosts: int) -> list[str]:
    """Filter to the hosts that actually answer on the web ports, capped at max_hosts."""
    sem = asyncio.Semaphore(50)

    async def _probe(h: str) -> str | None:
        async with sem:
            return await _is_live_web(h)

    live = [u for u in await asyncio.gather(*[_probe(h) for h in hosts]) if u]
    return live[:max_hosts] if max_hosts > 0 else live


def _emit(observe: ObserveFn, kind: str, data: dict[str, Any]) -> None:
    if observe:
        try:
            observe(kind, data)
        except Exception:  # noqa: BLE001 — telemetry must never break the hunt
            pass


async def hunt(target: str, *, enum: bool | None = None, max_hosts: int = 8,
               severity: str = "low,medium,high,critical", tags: str = "",
               observe: ObserveFn = None) -> dict[str, Any]:
    """Run a whole-program hunt and draft ONE combined report.

    target: a bare domain (auto-enumerated) or a URL/host (scanned directly).
    enum: force subdomain enumeration on/off; None = auto-detect from the target shape.
    Returns {target, hosts, findings, report}.
    """
    from friday.core.bounty import draft_bounty_report
    from friday.skills.recon import find_subdomains
    from friday.skills.web_recon import nuclei_scan

    target = (target or "").strip()
    if not target:
        return {"error": "no target to hunt"}
    bare = target.split("://")[-1].split("/")[0].split(":")[0].lower()
    do_enum = enum if enum is not None else bool(_BARE_DOMAIN.match(target.lower().rstrip("/")))

    if do_enum:
        _emit(observe, "enum", {"domain": bare})
        subs = (await find_subdomains(bare)).get("subdomains", [])
        if bare not in subs:
            subs = [bare, *subs]
        _emit(observe, "enumerated", {"count": len(subs)})
        hosts = await _live_web_hosts(subs, max_hosts)
    else:
        hosts = [target if target.startswith(("http://", "https://")) else "http://" + target]
    _emit(observe, "hosts", {"live": hosts})

    findings: list[dict[str, Any]] = []
    for h in hosts:
        _emit(observe, "scan", {"host": h})
        res = await nuclei_scan(h, severity=severity, tags=tags)
        host_findings = res.get("findings", [])
        for f in host_findings:
            f["host"] = h
            findings.append(f)
        _emit(observe, "scanned", {"host": h, "found": len(host_findings)})

    return {"target": target, "hosts": hosts, "findings": findings,
            "report": draft_bounty_report(target, findings)}
