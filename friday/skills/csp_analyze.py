"""Content-Security-Policy weakness analysis — parse a site's CSP and flag the gaps that let XSS
through anyway. A present CSP is a false comfort if it allows 'unsafe-inline', wildcard script
sources, or forgets base-uri/object-src; those are documented bypasses that turn a "mitigated" XSS
back into a working one.

Distinct from http_headers (which only notes CSP present/absent): this PARSES the policy and reports
each concrete weakness with severity. Deterministic (it reads the real header, no guessing).
PASSIVE: one ordinary GET → auto-confirmed. Pure-stdlib (urllib), CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import http.client
import urllib.error
import urllib.request
from typing import Any

from friday.skills.registry import skill


def _fetch_csp(url: str) -> tuple[str | None, int] | str:
    """GET the url; return (CSP header value or None, status) or an error string."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 sutra-csp/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:  # noqa: S310 — authorized target
            return _read_csp(r.headers), r.status
    except urllib.error.HTTPError as e:
        return _read_csp(e.headers), e.code
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        return f"could not reach {url}: {str(e)[:120]}"


def _read_csp(headers: Any) -> str | None:
    for k, v in headers.items():
        if k.lower() == "content-security-policy":
            return str(v)
    return None


def parse_csp(policy: str) -> dict[str, list[str]]:
    """Parse a CSP string into {directive: [sources]} (directive names lowercased)."""
    out: dict[str, list[str]] = {}
    for part in policy.split(";"):
        toks = part.split()
        if toks:
            out[toks[0].lower()] = toks[1:]
    return out


def analyze(policy: str) -> list[dict[str, str]]:
    """Flag concrete CSP weaknesses (deterministic) → list of {issue, severity, detail}."""
    d = parse_csp(policy)
    issues: list[dict[str, str]] = []
    # script-src (falls back to default-src) is where XSS lives
    script = d.get("script-src", d.get("default-src", []))
    script_l = [s.lower() for s in script]
    has_nonce_or_hash = any(s.startswith(("'nonce-", "'sha")) for s in script_l)

    if "'unsafe-inline'" in script_l and not has_nonce_or_hash:
        issues.append({"issue": "script-src allows 'unsafe-inline'", "severity": "high",
                       "detail": "Inline scripts execute — CSP does not stop reflected/stored XSS. "
                                 "Use nonces or hashes instead."})
    if "'unsafe-eval'" in script_l:
        issues.append({"issue": "script-src allows 'unsafe-eval'", "severity": "medium",
                       "detail": "eval()/Function() permitted — enables some XSS sink chains."})
    for wild in ("*", "https:", "http:", "data:"):
        if wild in script_l:
            issues.append({"issue": f"script-src allows wildcard source '{wild}'",
                           "severity": "high",
                           "detail": "Scripts can be loaded from any/most origins — trivially "
                                     "bypassable by hosting a payload on an allowed source."})
    if "object-src" not in d and "default-src" not in d:
        issues.append({"issue": "no object-src (and no default-src)", "severity": "medium",
                       "detail": "Plugins/embeds aren't restricted; set object-src 'none'."})
    elif "object-src" in d and "'none'" not in [s.lower() for s in d["object-src"]]:
        issues.append({"issue": "object-src is not 'none'", "severity": "low",
                       "detail": "Set object-src 'none' to block plugin-based vectors."})
    if "base-uri" not in d:
        issues.append({"issue": "no base-uri directive", "severity": "medium",
                       "detail": "A <base> tag injection can re-point relative script URLs — a "
                                 "known CSP bypass. Set base-uri 'none' or 'self'."})
    if "default-src" not in d and "script-src" not in d:
        issues.append({"issue": "no default-src or script-src", "severity": "high",
                       "detail": "The policy doesn't restrict script sources at all."})
    return issues


@skill(
    name="analyze_csp",
    description=(
        "Fetch a URL's Content-Security-Policy and analyse it for XSS-bypass weaknesses — "
        "'unsafe-inline'/'unsafe-eval', wildcard script sources (*, https:, data:), and missing "
        "object-src/base-uri/default-src. Unlike a header check that only notes CSP present/absent "
        "this PARSES the policy and reports each concrete gap with severity. Deterministic, "
        "passive (one GET). Authorized targets only."
    ),
    auto_confirm=True,  # passive: one benign GET, reads + parses the policy header
    timeout_s=20,
)
async def analyze_csp(url: str) -> dict[str, Any]:
    """Analyse a URL's CSP for bypass weaknesses.

    url: the target URL (a bare host gets http:// prepended).
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to analyse"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    res = await asyncio.to_thread(_fetch_csp, url)
    if isinstance(res, str):
        return {"error": res, "url": url}
    policy, status = res
    if not policy:
        return {"url": url, "status": status, "has_csp": False, "issues": [],
                "note": "No Content-Security-Policy header — the page has no CSP defence at all "
                        "(any XSS executes freely). Recommend adding a strict, nonce-based CSP."}
    issues = analyze(policy)
    return {
        "url": url,
        "status": status,
        "has_csp": True,
        "policy": policy[:500],
        "issue_count": len(issues),
        "issues": issues,
        "note": (
            f"{len(issues)} CSP weakness(es) found — the policy is present but bypassable; an XSS "
            "here is likely still exploitable. Prioritise the 'high' items."
            if issues else
            "CSP present with no obvious bypass in the checked directives. Still validate nonce "
            "handling and any allowed third-party origins manually."
        ),
    }
