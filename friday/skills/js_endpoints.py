"""JavaScript endpoint extraction — mining API paths and URLs out of a site's JS bundles.

extract_js_endpoints fetches a page and its linked scripts and regexes out every path, URL, and API
endpoint baked into the JavaScript (LinkFinder-style). Front-end JS is a map of the back-end: it
names /api/... routes, admin paths, and internal services that aren't linked in the HTML and that no
crawler reaches. Those endpoints are exactly what the injection skills (check_sqli / check_ssrf /
find_params) need as targets. PASSIVE: it fetches the target's own public assets with plain GETs, no
payloads (so auto_confirm, like scan_secrets / http_headers). Pure-stdlib (urllib), CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin, urlparse

from friday.skills.registry import skill

# Quoted strings in JS that look like a path, URL, or endpoint file.
_ENDPOINT_RE = re.compile(
    r"""['"]("""
    r"""(?:[a-zA-Z][a-zA-Z0-9+.\-]*://[^'"\s]{3,})"""              # absolute URL
    r"""|(?:/[A-Za-z0-9_./?&=%~\-]{2,})"""                          # absolute path /...
    r"""|(?:[A-Za-z0-9_./\-]+\.(?:php|aspx?|jsp|do|action|json|api)(?:\?[^'"\s]*)?)"""  # file ep
    r""")['"]""",
    re.IGNORECASE,
)
_SCRIPT_SRC_RE = re.compile(r"""<script[^>]+src=['"]([^'"]+)['"]""", re.IGNORECASE)
# Static-asset / noise extensions to drop — they're not endpoints.
_NOISE = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|ico|css|woff2?|ttf|eot|mp4|map|min\.js)(\?|$)", re.I)
_API_HINT = re.compile(
    r"/api/|/v\d+/|/rest/|/graphql|/admin|/internal|/wp-json|\.json|/oauth", re.I)
_MAX_SCRIPTS = 15


def _fetch(url: str) -> tuple[str, str | None]:
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-jsmap/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:  # noqa: S310 — authorized target
            return r.read(3_000_000).decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        try:
            return e.read(3_000_000).decode("utf-8", "replace"), None
        except Exception:  # noqa: BLE001
            return "", None
    except (urllib.error.URLError, OSError) as e:
        return "", str(e)[:160]


def _extract(text: str) -> list[str]:
    out = []
    for m in _ENDPOINT_RE.finditer(text or ""):
        ep = m.group(1).strip()
        if len(ep) < 3 or _NOISE.search(ep):
            continue
        out.append(ep)
    return out


@skill(
    name="extract_js_endpoints",
    description=(
        "Mine API paths, URLs and endpoints out of a site's JavaScript (LinkFinder-style). Fetches "
        "the page and its linked scripts and extracts every /api/... route, admin path, and "
        "internal endpoint baked into the JS — surface that isn't in the HTML and that crawlers "
        "miss. Feeds the injection skills (check_sqli / check_ssrf / find_params) real targets. "
        "Highlights API-looking endpoints. Passive — fetches the target's own public assets with "
        "plain GETs, no payloads."
    ),
    auto_confirm=True,  # passive: plain GETs of public assets, no payloads
    timeout_s=60,
)
async def extract_js_endpoints(url: str) -> dict[str, Any]:
    """Extract endpoints from a page's JavaScript.

    url: the target page URL (its <script src> bundles are fetched and mined too).
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to scan"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    def _run() -> tuple[list[str], int, bool]:
        page, err = _fetch(url)
        reachable = err is None
        endpoints = set(_extract(page))
        # also mine each linked script bundle
        srcs = _SCRIPT_SRC_RE.findall(page or "")
        scanned = 0
        for src in srcs[:_MAX_SCRIPTS]:
            js_url = urljoin(url, src)
            if urlparse(js_url).scheme not in ("http", "https"):
                continue
            body, jerr = _fetch(js_url)
            if jerr is None:
                reachable = True
            if body:
                scanned += 1
                endpoints.update(_extract(body))
        return sorted(endpoints), scanned, reachable

    endpoints, scanned, reachable = await asyncio.to_thread(_run)
    if not reachable:
        return {"error": f"could not reach {url}", "url": url}

    api = [e for e in endpoints if _API_HINT.search(e)]
    return {
        "url": url,
        "scripts_scanned": scanned,
        "total": len(endpoints),
        "api_endpoints": api[:100],
        "endpoints": endpoints[:300],
        "note": (
            f"{len(api)} API-looking endpoint(s) of {len(endpoints)} total. Resolve them against "
            "the host and feed to find_params / check_sqli / check_ssrf. JS often references "
            "routes that need auth or specific params — verify each is reachable before testing."
        ),
    }
