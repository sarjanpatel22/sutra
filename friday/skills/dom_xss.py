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

# Attacker-controllable DOM inputs — the taint origins.
_SOURCES = re.compile(
    r"\b(?:"
    r"location\.(?:hash|search|href|pathname)|document\.(?:URL|documentURI|baseURI|referrer)|"
    r"window\.name|location(?=\s*[.\[)])|(?:\.|\b)(?:hash|search)\b|"
    r"URLSearchParams|getParameter|\bevent\.data\b|\bmsg\.data\b|postMessage"
    r")",
    re.IGNORECASE,
)
# Dangerous DOM write/exec targets — where tainted data becomes script/markup.
_SINKS = re.compile(
    r"\b(?:"
    r"innerHTML|outerHTML|insertAdjacentHTML|document\.write(?:ln)?|"
    r"eval|Function\s*\(|setTimeout|setInterval|execScript|"
    r"\.html\s*\(|\.append\s*\(|\.after\s*\(|\.before\s*\(|"
    r"\.src\s*=|\.setAttribute|location\s*=|location\.href\s*=|location\.assign|location\.replace"
    r")",
    re.IGNORECASE,
)
_MAX_SCRIPTS = 15
_MAX_FINDINGS = 80


def _scan_js(source_name: str, text: str) -> list[dict[str, Any]]:
    """Find candidate source→sink flows in one JS body, line by line."""
    findings: list[dict[str, Any]] = []
    has_source_anywhere = bool(_SOURCES.search(text or ""))
    for n, line in enumerate(text.splitlines(), 1):
        sink = _SINKS.search(line)
        if not sink:
            continue
        src = _SOURCES.search(line)
        if src:  # source and sink on the SAME line — strongest signal
            findings.append({
                "file": source_name, "line": n, "confidence": "high",
                "source": src.group(0), "sink": sink.group(0),
                "snippet": line.strip()[:200],
            })
        elif has_source_anywhere:  # sink present, a source exists elsewhere in the file
            findings.append({
                "file": source_name, "line": n, "confidence": "low",
                "source": "(elsewhere in file)", "sink": sink.group(0),
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

    def _run() -> tuple[list[dict[str, Any]], int, bool]:
        page, err = _fetch(url)
        reachable = err is None
        findings = _scan_js("(inline)", page or "")
        scanned = 0
        for src in _SCRIPT_SRC_RE.findall(page or "")[:_MAX_SCRIPTS]:
            js_url = urljoin(url, src)
            if urlparse(js_url).scheme not in ("http", "https"):
                continue
            body, jerr = _fetch(js_url)
            if jerr is None:
                reachable = True
            if body:
                scanned += 1
                findings.extend(_scan_js(src.split("/")[-1] or src, body))
        return findings, scanned, reachable

    findings, scanned, reachable = await asyncio.to_thread(_run)
    if not reachable:
        return {"error": f"could not reach {url}", "url": url}

    findings = findings[:_MAX_FINDINGS]
    high = [f for f in findings if f["confidence"] == "high"]
    return {
        "url": url,
        "scripts_scanned": scanned,
        "count": len(findings),
        "high_confidence": len(high),
        "candidates": findings,
        "note": (
            f"{len(high)} high-confidence source→sink flow(s) of {len(findings)} candidate(s). "
            "These are STATIC candidates, not confirmed XSS — open the page and drive the source "
            "(e.g. put a payload in the URL #fragment / ?query) to confirm the sink executes it."
        ),
    }
