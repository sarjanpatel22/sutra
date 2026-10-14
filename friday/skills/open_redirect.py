"""Open-redirect testing — a payable class that chains into OAuth token theft, SSRF and phishing.

check_open_redirect injects an external canary URL into the redirect-style parameters of a target
(the ones present, or a small common set appended) and, WITHOUT following redirects, checks whether
the server sends a 3xx Location to the attacker host. A hit is a confirmed open redirect. Active
(sends crafted requests) so it is gated. Pure-stdlib (urllib), CI-safe, $0. Verify the redirect
actually fires in a browser / the real OAuth flow before reporting.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from friday.skills.registry import skill

_CANARY = "evil.example"
_PAYLOADS = (
    f"https://{_CANARY}/",
    f"//{_CANARY}/",
)  # absolute + scheme-relative (the classic bypass)
# Parameter names that commonly carry a redirect destination.
_REDIRECT_PARAMS = frozenset(
    {
        "next",
        "url",
        "redirect",
        "redirect_uri",
        "redirect_url",
        "redirecturl",
        "return",
        "returnurl",
        "return_url",
        "dest",
        "destination",
        "continue",
        "redir",
        "callback",
        "goto",
        "target",
        "link",
        "out",
        "to",
        "r",
        "u",
        "view",
        "page",
        "domain",
        "window",
        "checkout_url",
        "rurl",
    }
)
# When the URL carries no redirect-looking param, append-test this small high-signal set (bounded).
_DEFAULT_APPEND = ("next", "url", "redirect", "redirect_uri", "dest", "return")


def _with_param(url: str, param: str, value: str) -> str:
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q[param] = value
    return urlunparse(parts._replace(query=urlencode(q)))


def _redirects_to_canary(location: str | None) -> bool:
    if not location:
        return False
    loc = location.strip()
    probe = loc if "://" in loc else ("http:" + loc if loc.startswith("//") else loc)
    try:
        host = urlparse(probe).hostname
    except ValueError:
        host = None
    return host == _CANARY or loc.lower().lstrip().startswith(
        (f"https://{_CANARY}", f"http://{_CANARY}", f"//{_CANARY}")
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any) -> None:  # don't follow — we want the Location
        return None


def _probe(url: str) -> tuple[int, str | None, str | None]:
    """(status, Location, error) without following redirects."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-openredir/1.0"})
    try:
        with opener.open(req, timeout=10) as r:  # noqa: S310 — authorized target
            return r.status, r.headers.get("Location"), None
    except urllib.error.HTTPError as e:  # a blocked 3xx surfaces here, Location intact
        return e.code, e.headers.get("Location"), None
    except (urllib.error.URLError, OSError) as e:
        return 0, None, str(e)[:160]


@skill(
    name="check_open_redirect",
    description=(
        "Test a URL for an open redirect — chains into OAuth token theft, SSRF and "
        "phishing. Injects a canary URL into the redirect-style parameters (the ones in the "
        "URL, or a small common set appended) and, without following redirects, checks whether the "
        "server returns a 3xx Location to the attacker host. Reports each parameter that redirects "
        "off-site. Pass param= to test one parameter. Active (crafted requests) — authorized "
        "targets only; verify the redirect fires in a browser / the real flow before reporting."
    ),
    auto_confirm=False,  # sends crafted requests with payloads — gated
    timeout_s=60,
)
async def check_open_redirect(url: str, param: str = "") -> dict[str, Any]:
    """Probe a URL's redirect parameters for an open redirect.

    url: the target URL (ideally one with a redirect param, e.g. https://x/login?next=/home).
    param: optional single parameter to test; otherwise the URL's redirect-looking params are used,
        or a small common set is appended when none are present.
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
        candidates = [p for p in existing if p.lower() in _REDIRECT_PARAMS] or list(_DEFAULT_APPEND)

    def _run() -> tuple[list[dict[str, Any]], bool]:
        results: list[dict[str, Any]] = []
        reachable = False
        for p in candidates:
            for payload in _PAYLOADS:
                test_url = _with_param(url, p, payload)
                status, location, err = _probe(test_url)
                if err is None:
                    reachable = True
                if _redirects_to_canary(location):
                    results.append(
                        {
                            "param": p,
                            "payload": payload,
                            "status": status,
                            "location": location,
                            "test_url": test_url,
                        }
                    )
                    break  # this param is confirmed — no need to try the other payload
        return results, reachable

    hits, reachable = await asyncio.to_thread(_run)
    if not reachable:
        return {"error": f"could not reach {url}", "url": url}

    findings = [
        {
            "id": "open-redirect",
            "severity": "medium",
            "matched_at": h["test_url"],
            "name": f"Open redirect via '{h['param']}'",
            "description": (
                f"The '{h['param']}' parameter redirects off-site: payload '{h['payload']}' "
                f"produced HTTP {h['status']} Location: {h['location']}. Chains into OAuth "
                "redirect_uri token theft, SSRF, or phishing depending on where it's used."
            ),
            "curl": f"curl -i '{h['test_url']}'",
        }
        for h in hits
    ]

    return {
        "url": url,
        "tested_params": candidates,
        "hits": hits,
        "findings": findings,
        "vulnerable": bool(findings),
        "note": (
            "Confirm the redirect actually fires in a browser (and matters — e.g. an OAuth "
            "redirect_uri or login next=) before reporting; some redirects are harmless."
        ),
    }
