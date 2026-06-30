"""DOM-XSS source→sink analysis — statically scan a page's JavaScript for attacker-controllable
DOM *sources* (location.hash, document.URL, window.name, postMessage data…) flowing into dangerous
*sinks* (innerHTML, document.write, eval, setTimeout-string…). That source→sink pairing is the
signature of a DOM-based XSS — the class reflected_xss can't see because the payload never reaches
the server.

Static + deterministic + HONEST: it reports CANDIDATE flows (file, line, source, sink, snippet) but
does NOT claim execution — you confirm in a browser (e.g. `#<img src=x onerror=alert(1)>`). A flow
is ranked HIGH only when source and sink co-occur on one line. PASSIVE: plain GETs of the target's
own public JS, no payloads (auto_confirm, like extract_js_endpoints). Pure-stdlib, CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from friday.skills.js_endpoints import _SCRIPT_SRC_RE, _fetch
from friday.skills.registry import skill

# Attacker-controllable DOM inputs — the taint origins. Deliberately does NOT match a bare
# `.hash`/`.search` (those over-match String.search / Array.search / any `.hash` property) NOR a
# bare `location` (which self-matches the `location.replace`/`location.href=` SINKS); only the real
# DOM-source reads (location.hash, document.URL, window.name, postMessage data…) count.
_SOURCES = re.compile(
    r"\b(?:"
    r"location\.(?:hash|search|href|pathname)|document\.(?:URL|documentURI|baseURI|referrer)|"
    r"window\.name|"
    r"URLSearchParams|getParameter|\bevent\.data\b|\bmsg\.data\b|postMessage"
    r")",
    re.IGNORECASE,
)
# Dangerous DOM write/exec targets — where tainted data becomes script/markup. NOTE: `Function(` is
# CASE-SENSITIVE — `(?-i:Function)` — so the dangerous `new Function()` constructor matches but the
# ubiquitous lowercase `function(...)` expression does NOT (that was the biggest false-positive on
# minified bundles). The rest stay case-insensitive.
_SINKS = re.compile(
    r"\b(?:"
    r"innerHTML|outerHTML|insertAdjacentHTML|document\.write(?:ln)?|"
    r"eval\s*\(|(?-i:Function)\s*\(|setTimeout|setInterval|execScript|"
    r"\.html\s*\(|\.append\s*\(|\.after\s*\(|\.before\s*\(|"
    r"\.src\s*=|\.setAttribute|location\s*=|location\.href\s*=|location\.assign|location\.replace"
    r")",
    re.IGNORECASE,
)
_MAX_SCRIPTS = 15
_MAX_FINDINGS = 80
_PROXIMITY = 120  # chars: a source must be within this window of the sink to be a HIGH flow
_MAX_HIGH_PER_LINE = 3  # a single minified line can't flood the report with highs

# Minified/bundled library files where matched sinks are framework internals, not the app's
# attacker-reachable code — skipped to cut noise (the app's own bundle is still scanned).
_SKIP_BUNDLE = re.compile(
    r"(?:^|[/\-.])(?:runtime|polyfills?|vendors?|chunk-vendors|framework|webpack|"
    r"jquery|bootstrap|angular|react(?:-dom)?|vue|zone\.js|core-js|lodash|moment|popper|"
    r"tailwind|fontawesome)(?:[.\-]|$)", re.I)


def _scan_js(source_name: str, text: str) -> list[dict[str, Any]]:
    """Find candidate source→sink flows in one JS body.

    HIGH requires a source within `_PROXIMITY` chars of the sink (same statement) — not merely on
    the same physical line, which on MINIFIED code (the whole file is one line) would match a source
    and sink kilobytes apart and flag everything. LOW = a sink with a source elsewhere in the file.
    """
    findings: list[dict[str, Any]] = []
    has_source_anywhere = bool(_SOURCES.search(text or ""))
    for n, line in enumerate(text.splitlines(), 1):
        if not _SINKS.search(line):
            continue
        highs_this_line = 0
        for sm in _SINKS.finditer(line):
            window = line[max(0, sm.start() - _PROXIMITY): sm.end() + _PROXIMITY]
            srcm = _SOURCES.search(window)
            if not srcm:
                continue
            findings.append({
                "file": source_name, "line": n, "confidence": "high",
                "source": srcm.group(0), "sink": sm.group(0),
                "snippet": window.strip()[:200],
            })
            highs_this_line += 1
            if highs_this_line >= _MAX_HIGH_PER_LINE or len(findings) >= _MAX_FINDINGS:
                break
        if highs_this_line == 0 and has_source_anywhere:  # sink here, source elsewhere → weak lead
            findings.append({
                "file": source_name, "line": n, "confidence": "low",
                "source": "(elsewhere in file)", "sink": _SINKS.search(line).group(0),
                "snippet": line.strip()[:200],
            })
        if len(findings) >= _MAX_FINDINGS:
            break
    return findings


@skill(
    name="dom_xss_scan",
    description=(
        "Statically scan a page's JavaScript for DOM-XSS source→sink flows — attacker-controllable "
        "inputs (location.hash, document.URL, window.name, postMessage data) reaching dangerous "
        "sinks (innerHTML, document.write, eval, setTimeout-string). Finds client-side XSS that "
        "reflected/stored scanners miss (payload never hits the server). Reports CANDIDATE flows "
        "(file, line, snippet) to confirm in a browser — does NOT claim execution. Passive: plain "
        "GETs of the page and its scripts, no payloads. Authorized targets only."
    ),
    auto_confirm=True,  # passive static analysis: GETs public JS, sends no payloads
    timeout_s=60,
)
async def dom_xss_scan(url: str) -> dict[str, Any]:
    """Scan a page (and its linked scripts) for DOM-XSS source→sink candidates.

    url: target page URL; its inline scripts and <script src> bundles are fetched and analysed.
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to scan"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    def _run() -> tuple[list[dict[str, Any]], int, int, bool]:
        page, err = _fetch(url)
        reachable = err is None
        findings = _scan_js("(inline)", page or "")
        scanned = skipped = 0
        for src in _SCRIPT_SRC_RE.findall(page or "")[:_MAX_SCRIPTS]:
            js_url = urljoin(url, src)
            if urlparse(js_url).scheme not in ("http", "https"):
                continue
            name = src.split("?")[0].split("/")[-1] or src
            if _SKIP_BUNDLE.search(name):  # framework/library bundle → sinks are internals; skip
                skipped += 1
                continue
            body, jerr = _fetch(js_url)
            if jerr is None:
                reachable = True
            if body:
                scanned += 1
                findings.extend(_scan_js(name, body))
        return findings, scanned, skipped, reachable

    findings, scanned, skipped, reachable = await asyncio.to_thread(_run)
    if not reachable:
        return {"error": f"could not reach {url}", "url": url}

    findings = findings[:_MAX_FINDINGS]
    high = [f for f in findings if f["confidence"] == "high"]
    skip_note = f" {skipped} framework/library bundle(s) skipped as noise." if skipped else ""
    return {
        "url": url,
        "scripts_scanned": scanned,
        "skipped_bundles": skipped,
        "count": len(findings),
        "high_confidence": len(high),
        "candidates": findings,
        "note": (
            f"{len(high)} high-confidence source→sink flow(s) of {len(findings)} candidate(s)."
            f"{skip_note} These are STATIC candidates, not confirmed XSS — open the page and drive "
            "the source (e.g. a payload in the URL #fragment / ?query) to confirm the sink fires."
        ),
    }
