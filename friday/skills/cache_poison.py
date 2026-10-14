"""Web cache poisoning — does an unkeyed request header (X-Forwarded-Host, X-Host…) get reflected
into a response AND stored in the cache, so it's served to later visitors? A high-impact, payable
class (stored XSS / redirect to attacker infra, served from the CDN).

CONFIRMED-only, in two steps: (1) send the header with a unique canary and check it reflects;
(2) re-request the SAME url WITHOUT the header — if the canary still comes back, it was served from
cache → confirmed poisoning. Reflection that DOESN'T persist is reported as a weaker 'reflected, not
cached' candidate, never as confirmed.

SAFETY: every probe carries a unique cache-buster query param, so we only ever poison OUR OWN keyed
variant of the page — never the real shared cache that other users hit. Active (sends crafted
headers) → gated. Pure-stdlib (urllib), CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import http.client
import secrets
import urllib.error
import urllib.request
from typing import Any

from friday.skills.registry import skill

# Unkeyed-input headers a cache typically ignores for the key but the origin may reflect.
_UNKEYED_HEADERS = (
    "X-Forwarded-Host", "X-Host", "X-Forwarded-Server", "X-Original-Host",
    "X-Forwarded-Scheme", "X-Forwarded-Port",
)
# Response headers that corroborate the response came from a cache.
_CACHE_HINTS = ("x-cache", "cf-cache-status", "age", "x-cache-hits", "x-served-by", "cache-status")


def _req(url: str, extra: dict[str, str]) -> tuple[str, dict[str, str], int] | None:
    """GET url with extra headers; return (lowercased body, lowercased headers, status) or None."""
    headers = {"User-Agent": "Mozilla/5.0 sutra-cp/1.0", **extra}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as r:  # noqa: S310 — authorized target
            body = r.read(1_000_000).decode("utf-8", "replace").lower()
            return body, {k.lower(): v for k, v in r.headers.items()}, r.status
    except urllib.error.HTTPError as e:
        try:
            body = e.read(1_000_000).decode("utf-8", "replace").lower()
        except Exception:  # noqa: BLE001
            body = ""
        return body, {k.lower(): str(v) for k, v in e.headers.items()}, e.code
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return None


def _bust(url: str, token: str) -> str:
    """Append a unique cache-buster so we own a private cache key (never the shared one)."""
    return url + ("&" if "?" in url else "?") + f"sutracb={token}"


def _cache_evidence(headers: dict[str, str]) -> dict[str, str]:
    return {h: headers[h] for h in _CACHE_HINTS if h in headers}


def _probe(url: str) -> dict[str, Any]:
    """Try each unkeyed header for a reflect-then-persist cache poisoning. Sync (run in thread)."""
    candidate: dict[str, Any] | None = None
    for header in _UNKEYED_HEADERS:
        token = secrets.token_hex(6)
        canary = f"sutra{token}.poc.example"
        busted = _bust(url, token)  # unique key per header → isolated from the shared cache
        poisoned = _req(busted, {header: canary})
        if poisoned is None:
            continue
        body, _, _ = poisoned
        if canary not in body:
            continue  # header not reflected at all → nothing to cache
        # reflected → does it PERSIST without the header (i.e. was it cached)?
        clean = _req(busted, {})
        if clean is not None and canary in clean[0]:
            return {
                "vulnerable": True, "confirmed": True, "severity": "high",
                "header": header, "canary": canary, "url": busted,
                "cache_evidence": _cache_evidence(clean[1]),
            }
        if candidate is None:  # reflected but not cached — keep as a weaker lead
            candidate = {"header": header, "canary": canary, "url": busted}
    if candidate is not None:
        return {"vulnerable": False, "reflected_not_cached": True, **candidate}
    return {"vulnerable": False}


@skill(
    name="check_cache_poisoning",
    description=(
        "Test a URL for WEB CACHE POISONING: whether an unkeyed request header (X-Forwarded-Host, "
        "X-Host…) is reflected into the response AND cached, so it's served to other visitors. "
        "Confirmed-only — reflects a unique canary, then re-requests WITHOUT the header and "
        "confirms only if the canary persists (served from cache). Safe: every probe cache-busts "
        "so it poisons only its own private key, never the shared cache. Active — gated. "
        "Authorized targets that allow active testing only."
    ),
    auto_confirm=False,  # active: sends crafted headers
    timeout_s=60,
)
async def check_cache_poisoning(url: str) -> dict[str, Any]:
    """Probe `url` for reflect-and-cache poisoning via unkeyed headers.

    url: the target URL (a bare host gets http:// prepended). Cacheable pages (static-ish GET
        endpoints) are the right targets.
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to test"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    res = await asyncio.to_thread(_probe, url)
    if res.get("confirmed"):
        return {
            "url": url, "vulnerable": True, "severity": "high",
            "header": res["header"], "evidence": res["canary"],
            "cache_evidence": res.get("cache_evidence", {}),
            "findings": [{
                "id": f"cache-poison-{res['header'].lower()}",
                "name": f"Web cache poisoning via {res['header']}",
                "severity": "high", "matched_at": res["url"],
                "description": (
                    f"An unkeyed '{res['header']}' header was reflected and SERVED FROM CACHE on a "
                    "clean follow-up request (canary persisted without re-sending the header). An "
                    "attacker can poison the cache to serve their payload to other users. "
                    f"Reproduce: GET with '{res['header']}: {res['canary']}', then GET the URL "
                    "clean and observe the canary. Weaponise via a real host/XSS payload, in scope."
                ),
                "curl": f"curl -s '{res['url']}' -H '{res['header']}: {res['canary']}'",
            }],
            "note": "CONFIRMED cache poisoning — the unkeyed header persisted from cache.",
        }
    if res.get("reflected_not_cached"):
        return {
            "url": url, "vulnerable": False, "findings": [],
            "header": res["header"], "evidence": res["canary"],
            "note": (
                f"'{res['header']}' is REFLECTED but did not persist from cache here — a lead, not "
                "confirmed poisoning. Try a genuinely cacheable path (static asset, homepage) or "
                "other unkeyed inputs; the reflection itself may still enable an attack."
            ),
        }
    return {"url": url, "vulnerable": False, "findings": [],
            "note": "No unkeyed header reflected; no cache poisoning found on this URL."}
