"""Reflected-XSS probing — inject a unique marker with HTML metacharacters into parameters and
detect when it comes back UNENCODED in the response (a reflected-XSS candidate).

Deterministic: compares the reflection, never claims execution — you confirm the payload fires in a
browser and pick a working context. Active (sends payloads), gated. Pure-stdlib, CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import http.client
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from friday.skills.registry import skill

_MARKER = "sxss9q7z"  # unlikely to occur naturally; lets us locate our reflection in the body
_PROBE = f"{_MARKER}<svg>"  # the raw < > are the tell: an unencoded reflection means injectable
_PARAMS = frozenset({
    "q", "s", "search", "query", "keyword", "name", "message", "comment", "title", "lang",
    "redirect", "url", "return", "page", "id", "ref", "term", "text", "input", "value",
})
_DEFAULT_APPEND = ("q", "search", "name", "message", "id")


def _with_param(url: str, param: str, value: str) -> str:
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q[param] = value
    return urlunparse(parts._replace(query=urlencode(q)))


def _probe(url: str) -> tuple[str, str | None]:
    """(body, error). HTTP errors still return the body (the reflection can be on an error page)."""
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-xss/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 — authorized target
            return r.read(2_000_000).decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        try:
            return e.read(1_000_000).decode("utf-8", "replace"), None
        except Exception:  # noqa: BLE001
            return "", None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        return "", str(e)[:160]


@skill(
    name="test_reflected_xss",
    description=(
        "Test a URL's parameters for reflected XSS. Injects a unique marker containing HTML "
        "metacharacters and checks whether it returns UNENCODED (a raw <svg> in the response) — a "
        "reflected-XSS candidate — versus safely HTML-encoded. Tests the URL's params, or a small "
        "common set appended. Pass param= for one. Active, authorized only; confirm "
        "the payload actually executes in a browser before reporting."
    ),
    auto_confirm=False,  # sends payloads to the target — gated
    timeout_s=60,
)
async def test_reflected_xss(url: str, param: str = "") -> dict[str, Any]:
    """Probe a URL's parameters for unencoded reflection (a reflected-XSS candidate).

    url: the target URL (ideally one with a parameter, e.g. https://x/search?q=test).
    param: optional single parameter to test; else the URL's params or a small common set.
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to test"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    existing = [k for k, _ in parse_qsl(urlparse(url).query, keep_blank_values=True)]
    if param.strip():
        candidates = [param.strip()]
    else:
        candidates = [p for p in existing if p.lower() in _PARAMS] or list(_DEFAULT_APPEND)

    def _run() -> tuple[list[str], list[str], bool]:
        hits, encoded, reachable = [], [], False
        for p in candidates:
            body, err = _probe(_with_param(url, p, _PROBE))
            if err is None:
                reachable = True
            if _PROBE in (body or ""):  # raw <svg> reflected → injectable
                hits.append(p)
            elif _MARKER in (body or ""):  # reflected but encoded → note only
                encoded.append(p)
        return hits, encoded, reachable

    hits, encoded, reachable = await asyncio.to_thread(_run)
    if not reachable:
        return {"error": f"could not reach {url}", "url": url}

    findings = [
        {
            "id": "reflected-xss",
            "severity": "medium",
            "matched_at": _with_param(url, p, _PROBE),
            "name": f"Reflected XSS candidate via '{p}'",
            "description": (
                f"The '{p}' parameter reflected '{_PROBE}' UNENCODED (raw < and >) — the marker's "
                "HTML metacharacters survive into the response. Craft a payload for the context "
                "and confirm execution in a browser."
            ),
            "curl": f"curl -i '{_with_param(url, p, _PROBE)}'",
        }
        for p in hits
    ]

    return {
        "url": url,
        "tested_params": candidates,
        "reflected_unencoded": hits,
        "reflected_encoded": encoded,  # reflected but escaped — usually safe, listed for context
        "findings": findings,
        "vulnerable": bool(findings),
        "note": (
            "Candidates only — confirm the payload EXECUTES in a browser (right context, no CSP "
            "block) before reporting; encoded reflections are usually not exploitable."
        ),
    }
