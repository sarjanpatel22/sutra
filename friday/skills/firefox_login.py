"""Reuse your Firefox logins in Sutra's browser by copying its cookies.

Automation can't drive your *running* Firefox — it exposes no external debug channel,
and its profile is locked while it's open — so instead we read its cookie store, which
Firefox keeps unencrypted in cookies.sqlite, and load those cookies into Sutra's
browser context. Result: she's signed into the same sites you are, no second login.

Read-only against your profile (opened `immutable=1`, so it works even while Firefox
is running and never writes to it). Your own cookies, moving between your own browsers,
on your own machine. Only counts are ever returned — never the cookie values.
"""

from __future__ import annotations

import glob
import os
import sqlite3
from typing import Any
from urllib.parse import urlparse

from friday.skills.registry import skill


def _profile() -> str | None:
    """The Firefox profile dir to read (env override wins, else default-esr/default)."""
    override = os.getenv("FIREFOX_PROFILE_DIR")
    if override:
        return override if os.path.isdir(override) else None
    for pat in ("*.default-esr", "*.default-release", "*.default"):
        hits = sorted(glob.glob(os.path.expanduser(f"~/.mozilla/firefox/{pat}")))
        if hits:
            return hits[0]
    return None


def _registrable(domain: str) -> str:
    """Loose registrable form: lowercase, no leading dot, no www."""
    d = (domain or "").strip().lower().lstrip(".")
    return d[4:] if d.startswith("www.") else d


def _host_matches(host: str, want: str) -> bool:
    # bidirectional suffix match so apex cookies (.example.com) reach a subdomain
    # request (app.example.com) and vice-versa, without matching notexample.com
    h = host.lstrip(".").lower()
    return h == want or h.endswith("." + want) or want.endswith("." + h)


def domain_of(url_or_host: str) -> str:
    """Registrable domain from a URL or bare host (e.g. https://www.x.com/a → x.com)."""
    s = url_or_host.strip()
    host = urlparse(s).hostname if "//" in s else s.split("/")[0]
    return _registrable(host or "")


def read_firefox_cookies(domain: str = "") -> list[dict[str, Any]]:
    """Firefox cookies (optionally just one domain) as Playwright add_cookies dicts.

    Never raises and never returns values to callers' logs — the list is fed straight
    into context.add_cookies. Empty list when there's no profile / no match / on error.
    """
    prof = _profile()
    if not prof:
        return []
    db = os.path.join(prof, "cookies.sqlite")
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(
            "SELECT host,name,value,path,expiry,isSecure,isHttpOnly,sameSite FROM moz_cookies"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()

    want = _registrable(domain)
    out: list[dict[str, Any]] = []
    for host, name, value, path, expiry, secure, http_only, same in rows:
        if not host or (want and not _host_matches(host, want)):
            continue
        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": host,
            "path": path or "/",
            "secure": bool(secure),
            "httpOnly": bool(http_only),
            "expires": float(expiry) if expiry and expiry > 0 else -1,
        }
        # Firefox sameSite: 0=None, 1=Lax, 2=Strict. Playwright rejects None+insecure,
        # so only mark None when the cookie is secure; otherwise leave it unset.
        if same == 2:
            cookie["sameSite"] = "Strict"
        elif same == 1:
            cookie["sameSite"] = "Lax"
        elif secure:
            cookie["sameSite"] = "None"
        out.append(cookie)
    return out


async def apply_firefox_login(context: Any, domain: str = "") -> int:
    """Load your Firefox cookies (optionally one domain) into a Playwright context."""
    cookies = read_firefox_cookies(domain)
    if not cookies:
        return 0
    try:
        await context.add_cookies(cookies)
    except Exception:  # noqa: BLE001 — a sameSite combo Playwright dislikes; retry plainer
        for c in cookies:
            c.pop("sameSite", None)
        await context.add_cookies(cookies)
    return len(cookies)


@skill(
    name="import_firefox_login",
    description=(
        "Copy your Firefox cookies into Sutra's browser so she is logged into the same "
        "sites you are — no second login. ONLY for importing logins, NEVER for opening or "
        "launching Firefox ('open firefox' -> open_app). Give a domain (e.g. 'tryhackme.com') "
        "to import one site, or leave empty for all; use before a web_task on a signed-in site."
    ),
    auto_confirm=True,
    timeout_s=20,
)
async def import_firefox_login(domain: str = "") -> dict[str, Any]:
    """Bring your Firefox login(s) into Sutra's browser.

    domain: A site to import (e.g. "tryhackme.com"); empty imports every cookie.
    """
    from friday.skills.browser_session import get_page

    try:
        page = await get_page()
        n = await apply_firefox_login(page.context, domain)
    except Exception as e:  # noqa: BLE001 — surface a clean error
        return {"error": f"could not import Firefox login: {e}", "domain": domain or "(all)"}
    if not n:
        return {
            "status": "nothing imported",
            "domain": domain or "(all)",
            "note": "No Firefox profile found, or no cookies for that domain.",
        }
    return {
        "status": "ok",
        "imported": n,
        "domain": domain or "(all)",
        "note": (
            "Sutra's browser now shares your Firefox session for this site. If the page "
            "was already open, reload it to appear logged in."
        ),
    }
