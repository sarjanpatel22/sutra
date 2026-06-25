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
from urllib.parse import urlparse

ObserveFn = Callable[[str, dict[str, Any]], None] | None

# A bare apex/host (no scheme, port, or path) — the signal to enumerate subdomains.
_BARE_DOMAIN = re.compile(r"^(?!-)[a-z0-9-]+(?:\.[a-z0-9-]+)+$")


def _split(spec: str) -> list[str]:
    return [t for t in re.split(r"[,\s]+", (spec or "").strip().lower()) if t]


def _matches(host: str, pattern: str) -> bool:
    """A host matches a scope pattern under the bug-bounty convention that a listed domain covers
    its subdomains — so '*.example.com', 'example.com', and 'api.example.com' each mean 'this name
    and anything under it'. This is what keeps a hunt from wandering off the authorized scope."""
    host = host.lower().strip()
    pattern = pattern.lower().strip().lstrip("*").lstrip(".")
    return bool(pattern) and (host == pattern or host.endswith("." + pattern))


def _in_scope(host: str, scope: list[str], exclude: list[str]) -> bool:
    """In scope if it matches an allow pattern (or there are none) AND no deny pattern. Exclusion
    always wins — the safe default."""
    if any(_matches(host, p) for p in exclude):
        return False
    return (not scope) or any(_matches(host, p) for p in scope)


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


async def _url_reachable(url: str, timeout: float = 3.0) -> bool:
    """Can we open a TCP connection to the URL's host:port? Honors an EXPLICIT port (e.g. :3000),
    else the scheme default (443/80) — so a target on a non-standard port isn't wrongly judged
    'down' (the bug that made `sutra hunt http://host:3000` refuse a live target)."""
    parsed = urlparse(url if "://" in url else "http://" + url)
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except (OSError, TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


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


# The bug-bounty finding classes that actually PAY *and* are automatable — subdomain takeover,
# exposed secrets/config/backups, default logins, exposed panels. The default hunt targets these
# (the money) and stays FAST; 'cve' is a deeper opt-in pass (thousands of templates, slower) and
# 'all' is no tag filter. Info-level tech/fingerprint noise is deliberately excluded.
_PROFILES = {
    "high-value": "takeover,exposure,default-login,config,backup,panel,exposed-panel",
    "cve": "cve",
    "all": "",
}

# Sensitive paths that, if reachable, are real bounty material — content discovery (gobuster)
# surfaces the app-specific ones nuclei's generic templates never check. Tier the signal so the
# report triages honestly: HOT = secrets/backups/VCS (medium); WARM = panels/config/logs (low).
_HOT_PATH = re.compile(
    r"\.git|\.env|\.sql|\.bak|\.backup|backup|\.tar|\.zip|\.dump|id_rsa|\.htpasswd|"
    r"credential|secret|\.pem|\.key", re.I)
_WARM_PATH = re.compile(
    r"admin|/config|phpinfo|\.log|swagger|actuator|\.htaccess|console|/debug|/ftp", re.I)


async def _content_findings(host: str) -> tuple[list[dict[str, Any]], str | None]:
    """Content-brute a host → turn DISCOVERED sensitive paths into findings-to-verify. The
    app-specific coverage nuclei's templates miss (a forgotten /backup.zip, exposed /.git, a /ftp
    dir). Returns (findings, optional-warning)."""
    from friday.skills.web_recon import web_enum
    res = await web_enum(host)
    if res.get("error"):
        return [], f"content discovery skipped on {host}: {res['error']}"
    out: list[dict[str, Any]] = []
    for hit in res.get("found", []):
        path, status = hit.get("path", ""), hit.get("status")
        if status not in (200, 301, 302, 403):
            continue
        hot = bool(_HOT_PATH.search(path))
        if not (hot or _WARM_PATH.search(path)):
            continue
        url = host.rstrip("/") + path
        out.append({
            "id": f"content{path.rstrip('/').replace('/', '-') or '-root'}",
            "name": f"Discovered sensitive path {path} (HTTP {status})",
            "severity": "medium" if hot else "low",
            "matched_at": url,
            "description": "Surfaced by content discovery (gobuster), not a templated check — an "
                           "app-specific path nuclei may not cover. VERIFY it actually exposes "
                           "sensitive data or functionality before reporting.",
            "curl": f"curl -i '{url}'",
        })
    return out, None


async def hunt(target: str, *, enum: bool | None = None, max_hosts: int = 8,
               severity: str = "low,medium,high,critical", tags: str = "",
               profile: str = "high-value", scope: str = "", exclude: str = "",
               content: bool = False, observe: ObserveFn = None) -> dict[str, Any]:
    """Run a whole-program hunt and draft ONE combined report.

    target: a bare domain (auto-enumerated) or a URL/host (scanned directly).
    enum: force subdomain enumeration on/off; None = auto-detect from the target shape.
    profile: which template set to target — 'high-value' (default: the classes that pay), 'all'
        (everything), or a literal nuclei tag list. An explicit `tags=` always overrides it.
    scope: comma/space allow-list of in-scope domain patterns (default: the target apex only, so a
        hunt never wanders off the program). exclude: deny-list that always wins.
    content: opt-in deeper pass — brute-force each host for hidden paths/files (gobuster) and add
        discovered SENSITIVE ones (a forgotten /backup.zip, /.git) as findings nuclei misses.
        Slower and active; off by default so the standard hunt stays fast.
    Returns {target, hosts, findings, report}.
    """
    from friday.core.bounty import draft_bounty_report
    from friday.skills.recon import find_subdomains
    from friday.skills.web_recon import nuclei_scan

    target = (target or "").strip()
    if not target:
        return {"error": "no target to hunt"}
    bare = target.split("://")[-1].split("/")[0].split(":")[0].lower()
    scope_pats = _split(scope) or [bare]  # default: stay on the target's own domain
    excl_pats = _split(exclude)
    do_enum = enum if enum is not None else bool(_BARE_DOMAIN.match(target.lower().rstrip("/")))

    warnings: list[str] = []  # so '0 findings' is never silently mistaken for 'clean'
    if do_enum:
        _emit(observe, "enum", {"domain": bare})
        sub_res = await find_subdomains(bare)
        if sub_res.get("error"):  # a failed enum must NOT look like 'no assets'
            warnings.append(f"subdomain enumeration failed ({sub_res['error']}) — surface is "
                            f"incomplete; only the apex was scanned")
        subs = sub_res.get("subdomains", [])
        if bare not in subs:
            subs = [bare, *subs]
        in_scope = [s for s in subs if _in_scope(s, scope_pats, excl_pats)]
        _emit(observe, "enumerated", {"count": len(subs), "in_scope": len(in_scope)})
        hosts = await _live_web_hosts(in_scope, max_hosts)
    else:
        if not _in_scope(bare, scope_pats, excl_pats):
            return {"error": f"{bare} is outside the declared scope — refusing to scan it"}
        host_url = target if target.startswith(("http://", "https://")) else "http://" + target
        # liveness gate: don't burn a full scan on a host that isn't up — honors an explicit port
        if not await _url_reachable(host_url):
            return {"target": target, "hosts": [], "findings": [], "warnings": [],
                    "error": f"{bare} is not reachable — nothing to scan"}
        hosts = [host_url]
    _emit(observe, "hosts", {"live": hosts})

    scan_tags = (tags or "").strip() or _PROFILES.get(profile, profile)  # explicit tags win
    findings: list[dict[str, Any]] = []
    for h in hosts:
        _emit(observe, "scan", {"host": h})
        res = await nuclei_scan(h, severity=severity, tags=scan_tags)
        host_findings = res.get("findings", [])
        for f in host_findings:
            f["host"] = h
            findings.append(f)
        if res.get("incomplete"):  # timeout/no-templates — even WITH partial findings, flag it
            warnings.append(f"{h}: {res.get('note', 'scan incomplete')}")
        if content:  # opt-in: surface app-specific exposed paths nuclei's templates don't check
            _emit(observe, "content", {"host": h})
            cfinds, cwarn = await _content_findings(h)
            for f in cfinds:
                f["host"] = h
                findings.append(f)
            if cwarn:
                warnings.append(cwarn)
            _emit(observe, "content_found", {"host": h, "found": len(cfinds)})
        _emit(observe, "scanned", {"host": h, "found": len(host_findings)})

    return {"target": target, "hosts": hosts, "findings": findings, "warnings": warnings,
            "report": draft_bounty_report(target, findings, warnings=warnings)}
