"""Host-header injection testing — password-reset poisoning, web-cache poisoning, SSRF-ish routing.

check_host_header sends the request with a spoofed Host (and X-Forwarded-Host) header pointing at an
attacker canary, and — without following redirects — checks whether the app reflects that host into
the response Location or body (links, canonical tags, absolute URLs). Apps that build URLs from the
Host header let an attacker poison password-reset emails (the reset link points at the attacker →
account takeover) or the web cache. Active (crafted headers), gated. Pure-stdlib, CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import http.client
import urllib.error
import urllib.request
from typing import Any

from friday.skills.registry import skill

_CANARY = "evilcollab.example"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any) -> None:  # don't chase the spoofed host
        return None


def _probe(url: str, extra: dict[str, str]) -> tuple[int, str | None, str, str | None]:
    """(status, Location, body, error) with the given extra headers, not following redirects."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-hosthdr/1.0", **extra})
    try:
        with opener.open(req, timeout=10) as r:  # noqa: S310 — authorized target
            return (
                r.status,
                r.headers.get("Location"),
                r.read(500_000).decode("utf-8", "replace"),
                None,
            )
    except urllib.error.HTTPError as e:
        try:
            body = e.read(500_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, e.headers.get("Location"), body, None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        return 0, None, "", str(e)[:160]


@skill(
    name="check_host_header",
    description=(
        "Host-header injection — the password-reset-poisoning / web-cache-poisoning class. "
        "Sends the request with a spoofed Host and X-Forwarded-Host pointing at an attacker canary "
        "and checks whether the app reflects that host into the response Location or body (links, "
        "canonical, links). A reflection means the app trusts the Host header to build URLs — "
        "poison a password-reset link or the cache. Active (crafted headers) — "
        "authorized targets only; confirm via the real reset flow / cache before reporting."
    ),
    auto_confirm=False,  # crafted-header probes — gated
    timeout_s=40,
)
async def check_host_header(url: str) -> dict[str, Any]:
    """Probe a URL for Host / X-Forwarded-Host reflection.

    url: the target URL (ideally a page that builds links/redirects, e.g. a login or reset page).
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to test"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    vectors = [("Host", {"Host": _CANARY}), ("X-Forwarded-Host", {"X-Forwarded-Host": _CANARY})]

    def _run() -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        results: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        reachable = False
        for label, extra in vectors:
            status, location, body, err = _probe(url, extra)
            if err:
                results.append({"vector": label, "error": err})
                continue
            reachable = True
            loc_hit = bool(location and _CANARY in location)
            body_hit = _CANARY in (body or "")
            results.append(
                {
                    "vector": label,
                    "status": status,
                    "location_reflects": loc_hit,
                    "body_reflects": body_hit,
                }
            )
            if loc_hit:
                findings.append(
                    {
                        "id": "host-header-redirect",
                        "severity": "high",
                        "matched_at": url,
                        "name": f"{label} reflected into the redirect Location",
                        "description": (
                            f"Spoofing {label}: {_CANARY} made the server redirect to the "
                            f"attacker host (Location: {location}). The app trusts {label} to "
                            "build URLs — poison a password-reset link (account takeover)."
                        ),
                    }
                )
            elif body_hit:
                findings.append(
                    {
                        "id": "host-header-reflected",
                        "severity": "medium",
                        "matched_at": url,
                        "name": f"{label} reflected in the response body",
                        "description": (
                            f"Spoofing {label}: {_CANARY} appears in the response body — the "
                            "app builds absolute URLs/links from it. Check password-reset "
                            "emails and web-cache poisoning."
                        ),
                    }
                )
        return results, findings, reachable

    results, findings, reachable = await asyncio.to_thread(_run)
    if not reachable:
        return {"error": f"could not reach {url}", "url": url}

    order = {"high": 0, "medium": 1}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    return {
        "url": url,
        "results": results,
        "findings": findings,
        "vulnerable": bool(findings),
        "note": (
            "Confirm impact through the real flow — trigger a password reset and check the link "
            "host, or prove cache poisoning — before reporting; a reflected Host alone may be "
            "low-impact."
        ),
    }
