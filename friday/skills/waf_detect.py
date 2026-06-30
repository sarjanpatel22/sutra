"""WAF / CDN fingerprinting — identify the web application firewall or CDN in front of a target
from the fingerprints it leaks on a NORMAL response (headers, cookies, Server banner). Knowing the
WAF up front explains why probes get blocked and shapes the approach (bypass tactics, rate limits,
which payloads are wasted effort).

PASSIVE: a single ordinary GET, no attack-shaped traffic — fingerprints purely from what the WAF
adds to a benign response (cf-ray, x-iinfo, incap_ses cookie, Server: cloudflare…). So it's
auto-confirmed like http_headers — reports only what's positively fingerprinted, never guesses.
Pure-stdlib
(urllib), CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from typing import Any

from friday.skills.registry import skill

# WAF/CDN → the fingerprints it leaks. (header_name_lower, substring) matches a header VALUE;
# a header name alone (substring, "") matches the header's PRESENCE; cookie names match Set-Cookie.
_SIGNATURES: dict[str, dict[str, Any]] = {
    "Cloudflare": {"headers": [("server", "cloudflare"), ("cf-ray", ""), ("cf-cache-status", "")],
                   "cookies": ["__cfduid", "__cf_bm", "cf_clearance"]},
    "Akamai": {"headers": [("server", "akamaighost"), ("x-akamai-transformed", "")],
               "cookies": ["ak_bmsc", "_abck", "bm_sz"]},
    "Imperva Incapsula": {"headers": [("x-iinfo", ""), ("x-cdn", "incapsula")],
                          "cookies": ["incap_ses", "visid_incap", "nlbi_"]},
    "AWS CloudFront/WAF": {"headers": [("x-amz-cf-id", ""), ("via", "cloudfront"),
                                       ("server", "awselb")],
                           "cookies": ["AWSALB", "AWSALBCORS"]},
    "Sucuri": {"headers": [("server", "sucuri"), ("x-sucuri-id", ""), ("x-sucuri-cache", "")],
               "cookies": []},
    "F5 BIG-IP ASM": {"headers": [("server", "big-ip"), ("x-waf-status", "")],
                      "cookies": ["TS01", "BIGipServer", "F5_ST"]},
    "Fastly": {"headers": [("x-served-by", "cache-"), ("x-fastly-request-id", ""),
                           ("via", "varnish")],
               "cookies": []},
    "Barracuda": {"headers": [], "cookies": ["barra_counter_session", "BNI__BARRACUDA"]},
    "Wallarm": {"headers": [("server", "nginx-wallarm")], "cookies": []},
    "StackPath/MaxCDN": {"headers": [("server", "netdna"), ("x-hw", "")], "cookies": []},
    "Azure Front Door": {"headers": [("x-azure-ref", ""), ("x-cache", "tcp_hit")], "cookies": []},
}


def _fetch_meta(url: str) -> tuple[dict[str, str], str, int] | str:
    """GET the url; return (lowercased headers, raw Set-Cookie, status) or an error string."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 sutra-waf/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:  # noqa: S310 — authorized target
            return _collect(r.headers, r.status)
    except urllib.error.HTTPError as e:  # a 403/406 page still carries the WAF's fingerprints
        return _collect(e.headers, e.code)
    except (urllib.error.URLError, OSError) as e:
        return f"could not reach {url}: {str(e)[:120]}"


def _collect(headers: Any, status: int) -> tuple[dict[str, str], str, int]:
    hl = {k.lower(): v for k, v in headers.items()}
    cookies = "; ".join(v for k, v in headers.items() if k.lower() == "set-cookie")
    return hl, cookies, status


def _match(headers: dict[str, str], cookie_names: set[str]) -> list[dict[str, str]]:
    """Match the leaked fingerprints against the signature table → detected WAFs with evidence."""
    found: list[dict[str, str]] = []
    for waf, sig in _SIGNATURES.items():
        for name, needle in sig["headers"]:
            val = headers.get(name)
            if val is not None and (needle == "" or needle in val.lower()):
                found.append({"waf": waf, "evidence": f"header {name}: {val[:80]}"})
                break
        else:
            for ck in sig["cookies"]:
                if any(cn.startswith(ck) for cn in cookie_names):
                    found.append({"waf": waf, "evidence": f"cookie {ck}*"})
                    break
    return found


@skill(
    name="detect_waf",
    description=(
        "Fingerprint the WAF/CDN in front of a target (Cloudflare, Akamai, Imperva, AWS, Sucuri, "
        "F5, Fastly…) from a single NORMAL response — the headers/cookies it leaks (cf-ray, "
        "x-iinfo, incap_ses, Server banner). Knowing the WAF explains blocked probes and shapes "
        "the approach. PASSIVE: one ordinary GET, no attack traffic. Reports only positively "
        "fingerprinted WAFs, never guesses. Authorized targets only."
    ),
    auto_confirm=True,  # passive: one benign GET, fingerprints from what the WAF leaks
    timeout_s=20,
)
async def detect_waf(url: str) -> dict[str, Any]:
    """Identify the WAF/CDN fronting `url` from its response fingerprints.

    url: the target URL (a bare host gets http:// prepended).
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to fingerprint"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    meta = await asyncio.to_thread(_fetch_meta, url)
    if isinstance(meta, str):
        return {"error": meta, "url": url}
    headers, cookie_raw, status = meta
    cookie_names = set(SimpleCookie(cookie_raw).keys()) if cookie_raw else set()

    detected = _match(headers, cookie_names)
    # de-dupe by WAF name, keep first evidence
    seen: set[str] = set()
    unique = [d for d in detected if not (d["waf"] in seen or seen.add(d["waf"]))]
    return {
        "url": url,
        "status": status,
        "waf_detected": bool(unique),
        "wafs": unique,
        "note": (
            f"{len(unique)} WAF/CDN fingerprinted: {', '.join(d['waf'] for d in unique)}. Expect "
            "it to block/normalise payloads — plan bypasses and respect its rate limits."
            if unique else
            "No WAF/CDN positively fingerprinted from response metadata. The origin may still have "
            "filtering that only shows on a blocked (attack-shaped) request — absence isn't proof."
        ),
    }
