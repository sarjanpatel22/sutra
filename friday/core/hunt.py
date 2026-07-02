"""sutra hunt — the one-command bug-bounty program run.

Chains the pieces into a single flow over a whole program: find_subdomains + dns_brute (the assets)
+ a passive Shodan record → probe which are live → nuclei_scan each → ONE combined, submittable
bounty report. The TOOLS find (real, grounded output), the report assembles itself, YOU verify +
submit. Authorized programs only.

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

# A discovered param whose NAME reads like a chat/LLM message field — the only params worth firing
# the (active) prompt-injection probe at, so a hunt doesn't blast injections at every parameter.
_LLM_PARAM = re.compile(
    r"^(?:message|msg|prompt|query|q|question|input|text|content|chat|ask)$", re.I)


async def _shodan_leads(host: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Passive Shodan OSINT on the apex (run once per hunt, not per subdomain). Shodan-tagged CVEs
    become LOW leads (verify before reporting — a cached tag isn't a confirmed finding); the record
    summary is returned as context. Silently skipped when no API key is configured (CI-safe)."""
    from friday.skills.shodan_recon import shodan_host
    res = await shodan_host(host)
    if res.get("error"):  # no key / unresolved / API error → skip quietly
        return [], None
    leads = [
        {
            "id": f"shodan-{cve}", "name": f"Shodan-tagged {cve} on {res.get('ip', host)}",
            "severity": "low", "matched_at": res.get("ip", host), "host": host,
            "description": "Shodan tagged this CVE for the host from its cached scan data. A LEAD, "
                           "not a confirmed finding — re-verify the service/version is live and "
                           "actually vulnerable (nuclei / manual) before reporting.",
        }
        for cve in (res.get("vulns") or [])[:20]
    ]
    if not (leads or res.get("ports")):
        return [], None
    summary = {"ip": res.get("ip"), "ports": res.get("ports", []),
               "vulns": res.get("vulns", []), "org": res.get("org")}
    return leads, summary


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


async def _web_probe_findings(host: str, urls: list[str], max_urls: int = 8
                              ) -> tuple[list[dict[str, Any]], str | None]:
    """Run the targeted injection skills against a host: discover reflected params (find_params),
    then test each parameterised URL for SQLi / LFI / CRLF / SSRF, try XXE on crawled endpoints,
    check the host for CSRF, and discover its API/GraphQL surface (testing any GraphQL endpoint for
    batching abuse). Confirmed-only skills, so what they return is real (no scanner false-clean) —
    complementing nuclei (templated) and DAST (fuzzed). Returns (findings, optional-warning)."""
    from friday.skills.api_discovery import discover_api
    from friday.skills.cache_poison import check_cache_poisoning
    from friday.skills.crlf import check_crlf
    from friday.skills.csp_analyze import analyze_csp
    from friday.skills.csrf import check_csrf
    from friday.skills.dom_xss import dom_xss_scan
    from friday.skills.graphql_batch import graphql_batch
    from friday.skills.http_methods import check_http_methods
    from friday.skills.nosqli import check_nosqli
    from friday.skills.param_discovery import find_params
    from friday.skills.path_traversal import check_path_traversal
    from friday.skills.prompt_injection import check_prompt_injection
    from friday.skills.sqli import check_sqli
    from friday.skills.ssrf import check_ssrf
    from friday.skills.xxe import check_xxe

    findings: list[dict[str, Any]] = []
    # 1) discover reflected params on the host root → a synthesised parameterised test URL
    candidates: list[str] = []
    fp = await find_params(host)
    discovered = fp.get("discovered", [])
    if discovered:
        q = "&".join(f"{p}=1" for p in discovered[:6])
        candidates.append(f"{host.rstrip('/')}/?{q}")
    # 2) crawled URLs that already carry parameters
    candidates += [u for u in urls if "?" in u and "=" in u]
    cand = list(dict.fromkeys(candidates))[:max_urls]  # dedupe, preserve order, bound

    for u in cand:
        for fn in (check_sqli, check_nosqli, check_path_traversal, check_crlf, check_ssrf):
            res = await fn(u)
            findings.extend(res.get("findings", []))
    # 3) XXE against crawled endpoints (no params needed; confirmed-only, so safe to try), capped
    for u in [x for x in urls if "?" not in x][:max_urls]:
        res = await check_xxe(u)
        findings.extend(res.get("findings", []))
    # 4) CSRF on the host (state-changing forms without tokens / weak SameSite cookies)
    findings.extend((await check_csrf(host)).get("findings", []))
    # 5) API surface → GraphQL batching abuse on any live GraphQL endpoint found
    api = await discover_api(host)
    findings.extend(api.get("findings", []))
    for gql in api.get("graphql", [])[:max_urls]:
        findings.extend((await graphql_batch(gql)).get("findings", []))
    # 6) DOM-XSS: scan the host + a few crawled pages for high-confidence source→sink flows. These
    # are STATIC candidates (not confirmed execution), so only the strong ones go in, tagged.
    seen_dom: set[str] = set()  # the same JS file reached from two pages → one finding, not two
    for page in [host, *[u for u in urls if "?" not in u][:3]]:
        dom = await dom_xss_scan(page)
        for c in dom.get("candidates", []):
            if c.get("confidence") != "high":
                continue
            fid = f"domxss-{c.get('file', '')}-{c.get('line', 0)}"
            if fid in seen_dom:
                continue
            seen_dom.add(fid)
            findings.append({
                "id": fid,
                "name": f"Possible DOM XSS: {c.get('source')} → {c.get('sink')}",
                "severity": "low",
                "matched_at": page,
                "description": "Static source→sink flow in the page's JavaScript (not confirmed "
                               "execution). Drive the source (URL #fragment / ?query) in a browser "
                               f"to confirm. Snippet: {c.get('snippet', '')[:120]}",
            })
    # 7) Web cache poisoning on the host root (confirmed-only: reflect-then-persist, cache-busted)
    findings.extend((await check_cache_poisoning(host)).get("findings", []))
    # 8) HTTP method audit on the host (TRACE/XST confirmed by echo; dangerous methods advertised)
    findings.extend((await check_http_methods(host)).get("findings", []))
    # 9) CSP weakness: a present-but-bypassable policy leaves any XSS here exploitable. Only the
    # HIGH-severity gaps go in, as ONE low lead per host — hardening context that raises XSS impact,
    # not a bug on its own.
    csp_issues = (await analyze_csp(host)).get("issues", [])
    high_csp = [i for i in csp_issues if i.get("severity") == "high"]
    if high_csp:
        gaps = "; ".join(i["issue"] for i in high_csp)
        findings.append({
            "id": "csp-weak", "name": f"Weak CSP: {gaps[:120]}", "severity": "low",
            "matched_at": host,
            "description": f"Content-Security-Policy present but bypassable ({gaps}). Not a bug "
                           "alone, but it removes the mitigation for any XSS here — raise the "
                           "impact of a reflected/DOM XSS on this host accordingly.",
        })
    # 10) Prompt injection: if a discovered param looks like a chat/LLM message field, run the
    # echo-proof injection probe against it (confirmed-only → no false positive). One is enough.
    for p in discovered:
        if not _LLM_PARAM.match(p):
            continue
        pi = await check_prompt_injection(host, param=p)
        if pi.get("vulnerable"):
            findings.append({
                "id": f"prompt-injection-{p}", "name": f"Prompt injection via '{p}'",
                "severity": "high", "matched_at": host,
                "description": pi.get("note", "confirmed prompt injection (echo-proof reversal)"),
            })
        break
    return findings, None


async def hunt(target: str, *, enum: bool | None = None, max_hosts: int = 8,
               severity: str = "low,medium,high,critical", tags: str = "",
               profile: str = "high-value", scope: str = "", exclude: str = "",
               content: bool = False, active: bool = False, probe: bool = False,
               observe: ObserveFn = None) -> dict[str, Any]:
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
    active: opt-in DAST pass — nuclei's fuzzing engine actively probes parameters for SSRF/SQLi/
        injection with out-of-band proof (the payable classes, not brute-force). More intrusive;
        confirm the program ALLOWS active testing first.
    probe: opt-in targeted-injection pass — discovers reflected params (find_params) and runs the
        confirmed-only skills (check_sqli/nosqli/path_traversal/crlf/ssrf, check_xxe on crawled,
        check_csrf, API/GraphQL-batching, web cache poisoning, HTTP-method audit) against each
        parameterised URL, plus a static DOM-XSS source→sink scan of the host's JS (high-confidence
        flows only), a CSP-weakness check (bypassable policy → low lead), a WAF/CDN fingerprint
        (context), and prompt-injection on any chat-like param. Deterministic, signature-confirmed
        checks that complement nuclei + DAST. Authorized + active-testing-allowed only.
    Returns {target, hosts, findings, warnings, crawled, context, report}. `context` carries
        non-finding operator intel: a passive Shodan record (open ports / tagged CVEs) and any
        WAF/CDN fingerprinted per host.
    """
    from friday.core.bounty import draft_bounty_report
    from friday.skills.recon import find_subdomains
    from friday.skills.web_recon import crawl, nuclei_scan

    target = (target or "").strip()
    if not target:
        return {"error": "no target to hunt"}
    crawled: list[str] = []  # API endpoints pulled from each host (feed the coach the real surface)
    bare = target.split("://")[-1].split("/")[0].split(":")[0].lower()
    scope_pats = _split(scope) or [bare]  # default: stay on the target's own domain
    excl_pats = _split(exclude)
    do_enum = enum if enum is not None else bool(_BARE_DOMAIN.match(target.lower().rstrip("/")))

    warnings: list[str] = []  # so '0 findings' is never silently mistaken for 'clean'
    context: dict[str, Any] = {}  # operator intel that isn't a finding (WAF fronting / Shodan)
    shodan_leads: list[dict[str, Any]] = []
    if do_enum:
        _emit(observe, "enum", {"domain": bare})
        sub_res = await find_subdomains(bare)
        if sub_res.get("error"):  # a failed enum must NOT look like 'no assets'
            warnings.append(f"subdomain enumeration failed ({sub_res['error']}) — surface is "
                            f"incomplete; only the apex was scanned")
        subs = sub_res.get("subdomains", [])
        # DNS brute-force: surface live assets the passive sources miss (resolved-only, wildcard-
        # filtered), permuting the passive hits for env variants. Merged into the asset list.
        from friday.skills.dns_brute import dns_brute
        known = ",".join(subs[:50])
        brute = await dns_brute(bare, known=known)
        brute_hosts = [r["host"] for r in brute.get("subdomains", [])]
        if brute_hosts:
            _emit(observe, "dns_brute", {"found": len(brute_hosts)})
        if bare not in subs:
            subs = [bare, *subs]
        subs = list(dict.fromkeys([*subs, *brute_hosts]))  # merge, dedupe, preserve order
        in_scope = [s for s in subs if _in_scope(s, scope_pats, excl_pats)]
        _emit(observe, "enumerated", {"count": len(subs), "in_scope": len(in_scope)})
        hosts = await _live_web_hosts(in_scope, max_hosts)
        shodan_leads, shodan_ctx = await _shodan_leads(bare)  # passive OSINT on the apex, once
        if shodan_ctx:
            context["shodan"] = shodan_ctx
            _emit(observe, "shodan", {"ports": len(shodan_ctx["ports"]),
                                      "vulns": len(shodan_ctx["vulns"])})
    else:
        if not _in_scope(bare, scope_pats, excl_pats):
            return {"error": f"{bare} is outside the declared scope — refusing to scan it"}
        host_url = target if target.startswith(("http://", "https://")) else "http://" + target
        # liveness gate: don't burn a full scan on a host that isn't up — honors an explicit port
        if not await _url_reachable(host_url):
            return {"target": target, "hosts": [], "findings": [], "warnings": [],
                    "error": f"{bare} is not reachable — nothing to scan"}
        hosts = [host_url]
        shodan_leads, shodan_ctx = await _shodan_leads(bare)  # passive OSINT on the target, once
        if shodan_ctx:
            context["shodan"] = shodan_ctx
    _emit(observe, "hosts", {"live": hosts})

    scan_tags = (tags or "").strip() or _PROFILES.get(profile, profile)  # explicit tags win
    findings: list[dict[str, Any]] = list(shodan_leads)  # passive CVE leads seed the report
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
        if active:  # opt-in DAST: actively fuzz params for SSRF/injection (the payable classes)
            _emit(observe, "crawl", {"host": h})
            cr = await crawl(h)  # find the real surface (API endpoints hidden in the JS)
            crawled += cr.get("urls", [])
            _emit(observe, "crawled", {"host": h, "found": cr.get("count", 0)})
            _emit(observe, "active", {"host": h})
            ares = await nuclei_scan(h, severity=severity, active=True)
            afinds = ares.get("findings", [])
            for f in afinds:
                f["host"] = h
                f["active"] = True
                findings.append(f)
            if ares.get("incomplete"):
                warnings.append(f"{h} (active): {ares.get('note', 'scan incomplete')}")
            _emit(observe, "active_found", {"host": h, "found": len(afinds)})
        if probe:  # opt-in: targeted confirmed-only injection probes (find_params → sqli/lfi/crlf)
            _emit(observe, "probe", {"host": h})
            from friday.skills.waf_detect import detect_waf
            waf = await detect_waf(h)  # passive: what's fronting this host shapes the approach
            if waf.get("waf_detected"):
                context.setdefault("waf", {})[h] = [d["waf"] for d in waf["wafs"]]
                _emit(observe, "waf", {"host": h, "wafs": context["waf"][h]})
            host_urls = [u for u in crawled if urlparse(u).hostname == urlparse(h).hostname]
            if not host_urls:  # no crawl ran (probe without active) — crawl this host now
                cr = await crawl(h)
                host_urls = cr.get("urls", [])
                crawled += host_urls
            pfinds, pwarn = await _web_probe_findings(h, host_urls)
            for f in pfinds:
                f["host"] = h
                f.setdefault("probe", True)
                findings.append(f)
            if pwarn:
                warnings.append(pwarn)
            _emit(observe, "probe_found", {"host": h, "found": len(pfinds)})
        _emit(observe, "scanned", {"host": h, "found": len(host_findings)})

    return {"target": target, "hosts": hosts, "findings": findings, "warnings": warnings,
            "crawled": crawled, "context": context,
            "report": draft_bounty_report(target, findings, warnings=warnings,
                                          surfaces=hosts + crawled)}
