"""CORS misconfiguration testing — the cross-origin data-theft class.

check_cors sends requests with attacker-controlled Origin headers (an arbitrary external origin,
'null', and a subdomain-style bypass derived from the host) and inspects the Access-Control-Allow-
Origin / -Allow-Credentials response. The payable case: the server REFLECTS an arbitrary origin AND
allows credentials — any attacker page can make credentialed requests and read the authenticated
response (account-takeover data theft). Lighter cases (null origin, wildcard) are flagged lower.
Pure-stdlib GETs (no attack, just crafted Origin headers); verify against a real authenticated
endpoint before reporting. CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from friday.skills.registry import skill

_ARBITRARY = "https://attacker.example"  # an origin the target has no reason to trust


def _origins(url: str) -> list[str]:
    """Default attacker origins to test: arbitrary external, null, and a host-suffix bypass that
    catches a naive endswith()/contains() origin check."""
    host = urlparse(url).hostname or ""
    out = [_ARBITRARY, "null"]
    if host:
        out.append(f"https://{host}.attacker.example")  # startswith/contains bypass
    return out


def _probe(url: str, origin: str) -> tuple[int, str | None, str | None, str | None]:
    """(status, ACAO, ACAC, error) for one Origin. A 4xx/5xx carries CORS headers worth seeing;
    only a transport failure returns an error."""
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-cors/1.0", "Origin": origin})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 — authorized target
            h = r.headers
            return (
                r.status,
                h.get("Access-Control-Allow-Origin"),
                h.get("Access-Control-Allow-Credentials"),
                None,
            )
    except urllib.error.HTTPError as e:
        return (
            e.code,
            e.headers.get("Access-Control-Allow-Origin"),
            e.headers.get("Access-Control-Allow-Credentials"),
            None,
        )
    except (urllib.error.URLError, OSError) as e:
        return 0, None, None, str(e)[:160]


@skill(
    name="check_cors",
    description=(
        "Test a URL for CORS misconfiguration — the cross-origin data-theft class. Replays the "
        "request with attacker Origin headers (an arbitrary external origin, 'null', and a "
        "subdomain-style bypass) and inspects Access-Control-Allow-Origin / -Allow-Credentials. "
        "Flags the payable case — REFLECTS an arbitrary origin AND allows credentials, so "
        "any attacker page can read the authenticated response — plus lighter null-origin and "
        "wildcard cases. Pass origins= (comma list) to test custom bypasses. Verify against a real "
        "authenticated endpoint before reporting."
    ),
    auto_confirm=True,  # a few GETs with crafted Origin headers — read-only CORS inspection
    timeout_s=40,
)
async def check_cors(url: str, origins: str = "") -> dict[str, Any]:
    """Probe a URL's CORS policy with attacker origins.

    url: the endpoint to test (ideally one that returns user-specific data; a bare host -> http://).
    origins: optional comma-separated custom origins to test instead of the built-in defaults.
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to test"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    test_origins = [o.strip() for o in origins.split(",") if o.strip()] or _origins(url)

    def _run() -> list[tuple[str, tuple[int, str | None, str | None, str | None]]]:
        return [(o, _probe(url, o)) for o in test_origins]

    probed = await asyncio.to_thread(_run)
    if all(res[3] for _, res in probed):  # every probe was a transport error → target unreachable
        return {"error": f"could not reach {url}: {probed[0][1][3]}", "url": url}

    results: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for origin, (status, acao, acac, err) in probed:
        if err:
            results.append({"origin": origin, "error": err})
            continue
        cred = (acac or "").strip().lower() == "true"
        entry = {"origin": origin, "status": status, "acao": acao, "credentials": cred}
        reflected = acao is not None and acao == origin
        if reflected and origin == _ARBITRARY:
            sev = "critical" if cred else "medium"
            entry["verdict"] = f"reflects an arbitrary origin{' WITH credentials' if cred else ''}"
            findings.append(
                {
                    "id": "cors-reflects-arbitrary-origin",
                    "severity": sev,
                    "matched_at": url,
                    "name": f"CORS reflects arbitrary origin{' with credentials' if cred else ''}",
                    "description": (
                        f"The server set Access-Control-Allow-Origin to '{origin}' (reflecting "
                        f"a value an attacker controls)"
                        + (
                            " AND Allow-Credentials: true — an attacker page can make "
                            "cross-origin requests and read the authenticated response (data "
                            "theft / account takeover). Verify against a user-data endpoint."
                            if cred
                            else " (no credentials) — any origin reads it cross-origin; impactful "
                            "only if it serves sensitive data."
                        )
                    ),
                }
            )
        elif acao == "null":
            sev = "high" if cred else "low"
            entry["verdict"] = "allows the null origin"
            findings.append(
                {
                    "id": "cors-null-origin",
                    "severity": sev,
                    "matched_at": url,
                    "name": "CORS allows the 'null' origin",
                    "description": (
                        f"Access-Control-Allow-Origin: null (credentials={cred}). A sandboxed "
                        "iframe/data-URL gets origin 'null' and can read the response. Verify."
                    ),
                }
            )
        elif reflected:  # a non-trusted (non-arbitrary, non-null) origin was reflected → bypass
            sev = "high" if cred else "medium"
            entry["verdict"] = "reflects a non-trusted origin (bypass)"
            findings.append(
                {
                    "id": "cors-origin-bypass",
                    "severity": sev,
                    "matched_at": url,
                    "name": "CORS reflects a non-trusted origin (validation bypass)",
                    "description": (
                        f"The server reflected '{origin}', which it should not trust — a flawed "
                        f"origin check (endswith/contains/regex). credentials={cred}. Verify."
                    ),
                }
            )
        elif acao == "*":
            entry["verdict"] = "wildcard ACAO"
            findings.append(
                {
                    "id": "cors-wildcard",
                    "severity": "low",
                    "matched_at": url,
                    "name": "CORS wildcard (Access-Control-Allow-Origin: *)",
                    "description": (
                        "Any origin can read this response. Browsers ignore '*' with "
                        "credentials, so impact is limited to whatever this endpoint exposes "
                        "unauthenticated — confirm it's sensitive before reporting."
                    ),
                }
            )
        else:
            entry["verdict"] = "no permissive CORS for this origin"
        results.append(entry)

    # Worst-first; one finding per origin already, so no dedup needed.
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    return {
        "url": url,
        "tested_origins": test_origins,
        "results": results,
        "findings": findings,
        "vulnerable": any(f["severity"] in ("critical", "high", "medium") for f in findings),
        "note": (
            "Confirm impact on an endpoint that returns USER-SPECIFIC data with the victim's "
            "credentials — a permissive CORS header on a public endpoint is not a finding."
        ),
    }
