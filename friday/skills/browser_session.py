"""Sutra's own browser — one persistent, visible Chromium she drives.

The action skills used to each spin up a throwaway *headless* browser, so what
Sutra opened (`open_url` → your Firefox) and what she acted in (a hidden Chromium)
were different programs — nothing she did was logged in or on screen. This gives
her a single persistent browser with its own on-disk profile: you log into a site
once, by hand, in this window, and every later `web_task` reuses that session.

Headed by default so you can watch her work — and so the one-time login is yours,
done in a real window, never a password typed through chat. Falls back to headless
only when there's no display (servers, CI). The whole thing stays $0.

The Playwright objects bind to the event loop that created them, so the session is
keyed to its loop and transparently rebuilt if reused from a different one (e.g. the
dashboard's bridge loop vs. a one-shot CLI loop).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from friday.skills.browse import _NAV_TIMEOUT_MS, _http_url
from friday.skills.registry import skill

_PROFILE_DIR = Path.home() / ".local" / "share" / "friday" / "sutra-browser"
_VIEWPORT = {"width": 1366, "height": 880}

_pw: Any = None
_context: Any = None
_loop: asyncio.AbstractEventLoop | None = None


def _headless() -> bool:
    """Headed needs an X/Wayland display; fall back to headless without one so the
    skill still works (just invisibly). SUTRA_BROWSER_HEADLESS forces the choice."""
    env = os.getenv("SUTRA_BROWSER_HEADLESS")
    if env is not None:
        return env not in ("", "0", "false", "no")
    return not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))


async def _reset() -> None:
    """Tear down the current session, swallowing cross-loop/teardown errors."""
    global _pw, _context, _loop
    for closer in (
        lambda: _context and _context.close(),
        lambda: _pw and _pw.stop(),
    ):
        try:
            coro = closer()
            if coro:
                await coro
        except Exception:  # noqa: BLE001 — best-effort teardown, never raise
            pass
    _pw = _context = _loop = None


async def get_page() -> Any:
    """A live Page in Sutra's persistent browser, launching/reusing as needed."""
    global _pw, _context, _loop
    running = asyncio.get_running_loop()
    if _context is not None and _loop is running:
        try:
            return _context.pages[-1] if _context.pages else await _context.new_page()
        except Exception:  # noqa: BLE001 — context died; rebuild below
            await _reset()
    elif _context is not None:
        await _reset()  # adopted by a different loop — rebuild on this one

    from playwright.async_api import async_playwright

    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _pw = await async_playwright().start()
    _context = await _pw.chromium.launch_persistent_context(
        user_data_dir=str(_PROFILE_DIR),
        headless=_headless(),
        viewport=_VIEWPORT,
        args=[
            "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
            # a visible floating window (not maximized over the dashboard) so you can
            # see her work and still get back to the dashboard behind it
            f"--window-size={_VIEWPORT['width']},{_VIEWPORT['height']}",
            "--window-position=140,90",
        ],
    )
    _loop = running
    return _context.pages[0] if _context.pages else await _context.new_page()


async def close_session() -> None:
    """Close Sutra's browser (called on dashboard/CLI shutdown). Safe if never opened."""
    await _reset()


@skill(
    name="browser_open",
    description=(
        "Open a URL in Sutra's own persistent, visible browser — the same browser "
        "`web_task` drives. Reuses your Firefox login for the site by default, so she's "
        "already signed in. Use it to show the user a page, or to land on a site before a "
        "web_task. Reversible — just navigates."
    ),
    auto_confirm=True,
    timeout_s=45,
)
async def browser_open(url: str, use_my_login: bool = True) -> dict[str, Any]:
    """Open a page in Sutra's browser.

    url: An http:// or https:// URL to open.
    use_my_login: Reuse your Firefox login for this site (default true).
    """
    if not _http_url(url):
        return {"error": "only http(s) URLs are allowed", "url": url}
    try:
        page = await get_page()
        if use_my_login:
            try:
                from friday.skills.firefox_login import apply_firefox_login, domain_of

                await apply_firefox_login(page.context, domain_of(url))
            except Exception:  # noqa: BLE001 — login reuse is best-effort
                pass
        await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        return {
            "status": "ok",
            "url": page.url,
            "title": await page.title(),
            "note": (
                "Opened in Sutra's browser, reusing your Firefox login where available. "
                "If it still shows logged-out, sign in here once and it'll stick."
            ),
        }
    except Exception as e:  # noqa: BLE001 — surface a clean error
        return {"error": f"could not open page: {e}", "url": url}
