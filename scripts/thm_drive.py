"""Manual TryHackMe driver — Claude is the agent.

Opens Sutra's persistent, VISIBLE Chromium with the user's Firefox login
transplanted in (so we're already signed in — no Google flow), navigates to a
URL, screenshots it, and prints the clickable elements. Claude reads the shot,
decides the next step, and re-runs with a new URL or a click target.

Usage:
    python scripts/thm_drive.py <url> [click=<substring>] [shot=/tmp/thm.jpg]

State persists in the on-disk profile, so logins carry across runs.
"""

from __future__ import annotations

import asyncio
import sys

from friday.skills.browser_session import close_session, get_page
from friday.skills.firefox_login import apply_firefox_login, domain_of

_SHOT = "/tmp/thm.jpg"


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://tryhackme.com/dashboard"
    click = next((a.split("=", 1)[1] for a in sys.argv[2:] if a.startswith("click=")), None)
    shot = next((a.split("=", 1)[1] for a in sys.argv[2:] if a.startswith("shot=")), _SHOT)

    page = await get_page()
    n = await apply_firefox_login(page.context, domain_of(url))
    print(f"login cookies in context: {n}")
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2800)  # let the SPA settle

    if click:
        el = page.get_by_text(click, exact=False).first
        try:
            await el.click(timeout=8000)
            await page.wait_for_timeout(2800)
            print(f"clicked: {click!r}")
        except Exception as e:  # noqa: BLE001
            print(f"click {click!r} failed: {e}")

    await page.screenshot(path=shot, type="jpeg", quality=72, full_page=False)
    print(f"URL: {page.url}")
    print(f"TITLE: {await page.title()}")

    # Visible links/buttons with their text — so Claude can choose the next move.
    items = await page.evaluate(
        r"""() => {
      const out = [];
      for (const e of document.querySelectorAll('a,button,[role=button]')) {
        const r = e.getBoundingClientRect();
        if (r.width < 6 || r.height < 6 || r.top > innerHeight || r.bottom < 0) continue;
        const t = (e.innerText || e.getAttribute('aria-label') || '').replace(/\s+/g,' ').trim();
        if (!t) continue;
        out.push({t: t.slice(0, 60), href: e.getAttribute('href') || ''});
      }
      return out.slice(0, 60);
    }"""
    )
    print("--- clickable ---")
    seen = set()
    for it in items:
        key = (it["t"], it["href"])
        if key in seen:
            continue
        seen.add(key)
        print(f"  {it['t']!r}  {it['href']}")
    await close_session()


if __name__ == "__main__":
    asyncio.run(main())
