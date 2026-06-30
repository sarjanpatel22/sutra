"""CSRF exposure check — state-changing forms without anti-CSRF tokens + weak SameSite cookies.

check_csrf fetches a page and flags the two ingredients of a Cross-Site Request Forgery: a
state-changing (POST) HTML form that carries NO anti-CSRF token field, and session cookies set
without a protective SameSite attribute (or SameSite=None without Secure, which is cross-site
sendable). Together they let an attacker's page submit authenticated requests on a victim's behalf.
PASSIVE: one GET, no state change (so auto_confirm). Findings are candidates — confirm the action is
genuinely state-changing and lacks other defences (custom header / re-auth) before reporting.
Pure-stdlib (urllib + regex), CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.request
from typing import Any

from friday.skills.registry import skill

_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.IGNORECASE | re.DOTALL)
_METHOD_POST = re.compile(r"""method\s*=\s*['"]?\s*post""", re.IGNORECASE)
_ACTION_RE = re.compile(r"""action\s*=\s*['"]([^'"]*)['"]""", re.IGNORECASE)
# A hidden/any input whose NAME looks like an anti-CSRF token.
_TOKEN_FIELD = re.compile(
    r"""<input\b[^>]*\bname\s*=\s*['"]?[^'">]*"""
    r"""(?:csrf|xsrf|_token|authenticity_token|nonce|anti.?forgery|requestverificationtoken)""",
    re.IGNORECASE,
)
_SESSION_COOKIE = re.compile(r"(?i)(session|sess|sid|auth|jwt|token|login|remember)")


def _fetch(url: str) -> tuple[str, list[str], str | None]:
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-csrf/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 — authorized target
            cookies = r.headers.get_all("Set-Cookie") or []
            return r.read(3_000_000).decode("utf-8", "replace"), cookies, None
    except urllib.error.HTTPError as e:
        try:
            cookies = e.headers.get_all("Set-Cookie") or []
            return e.read(2_000_000).decode("utf-8", "replace"), cookies, None
        except Exception:  # noqa: BLE001
            return "", [], None
    except (urllib.error.URLError, OSError) as e:
        return "", [], str(e)[:160]


@skill(
    name="check_csrf",
    description=(
        "Check a page for CSRF exposure. Flags state-changing (POST) HTML forms that carry NO "
        "anti-CSRF token field, and session cookies set without a protective SameSite attribute "
        "(or SameSite=None without Secure). Together these enable Cross-Site Request Forgery — an "
        "attacker page submitting authenticated actions for a victim. Passive (one GET, no state "
        "change). Findings are candidates: confirm the action is state-changing and lacks other "
        "defences (custom header / re-auth) before reporting."
    ),
    auto_confirm=True,  # one read-only GET, no state change → passive
    timeout_s=30,
)
async def check_csrf(url: str) -> dict[str, Any]:
    """Inspect a page for missing CSRF protections.

    url: the target page URL (ideally one with a form, e.g. a settings/profile/transfer page).
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to check"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    body, cookies, err = await asyncio.to_thread(_fetch, url)
    if err is not None:
        return {"error": f"could not reach {url}: {err}", "url": url}

    findings: list[dict[str, Any]] = []

    post_forms = 0
    unprotected = 0
    for attrs, inner in _FORM_RE.findall(body or ""):
        if not _METHOD_POST.search(attrs):
            continue  # only POST forms are state-changing / CSRF-relevant
        post_forms += 1
        if _TOKEN_FIELD.search(inner):
            continue  # has an anti-CSRF token field
        unprotected += 1
        action = (_ACTION_RE.search(attrs) or [None, "(same page)"])[1]
        findings.append({
            "id": "csrf-missing-token",
            "severity": "medium",
            "matched_at": url,
            "name": f"POST form without an anti-CSRF token (action: {action})",
            "description": (
                f"A state-changing POST form (action='{action}') on {url} has no anti-CSRF token "
                "field. If the action lacks other defences (custom header, re-auth) it is "
                "CSRF-able — an attacker page can submit it for a logged-in victim. Confirm the "
                "action changes state and forge a cross-site request to prove it."
            ),
        })

    for c in cookies:
        name = c.split("=", 1)[0].strip()
        low = c.lower()
        if not _SESSION_COOKIE.search(name):
            continue
        if "samesite=none" in low and "secure" not in low:
            findings.append({
                "id": "cookie-samesite-none-insecure",
                "severity": "medium",
                "matched_at": url,
                "name": f"Session cookie '{name}' is SameSite=None without Secure",
                "description": (
                    f"Cookie '{name}' is sent cross-site (SameSite=None) but not marked Secure — "
                    "it rides along on attacker-initiated cross-site requests, enabling CSRF."
                ),
            })
        elif "samesite" not in low:
            findings.append({
                "id": "cookie-no-samesite",
                "severity": "low",
                "matched_at": url,
                "name": f"Session cookie '{name}' has no SameSite attribute",
                "description": (
                    f"Cookie '{name}' sets no SameSite attribute. Browsers default to Lax, but "
                    "explicit SameSite=Lax/Strict is the defence — combined with a tokenless POST "
                    "form this raises CSRF risk."
                ),
            })

    order = {"medium": 0, "low": 1}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    return {
        "url": url,
        "post_forms": post_forms,
        "unprotected_forms": unprotected,
        "cookies_checked": len(cookies),
        "findings": findings,
        "vulnerable": bool(findings),
        "note": (
            "CSRF can't be fully confirmed from the page alone — verify each flagged action is "
            "state-changing and lacks a custom-header/re-auth defence, then forge a cross-site "
            "request to prove it before reporting."
        ),
    }
