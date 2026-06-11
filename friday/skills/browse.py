"""Browse a web page with a headless browser and return its readable text.

Playwright drives a real headless Chromium, so this reads JS-rendered pages a plain
HTTP fetch can't — the deterministic, token-cheap half of Phase 1.5 (no LLM in the
loop, unlike autonomous browser-use). Read-only: it loads and reads a page you name,
never clicks or submits, so it auto-runs. Returns the page title plus visible text,
whitespace-normalized and truncated to keep the model's context cheap.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from friday.skills.registry import skill

_MAX_CHARS = 6000
_NAV_TIMEOUT_MS = 30_000


def _http_url(url: str) -> bool:
    """Only http(s) — never file:, javascript:, data:, etc., into a real browser."""
    try:
        p = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def _readable(raw: str) -> str:
    """Collapse a page's inner_text into non-blank, stripped lines (keeps structure)."""
    lines = (ln.strip() for ln in raw.splitlines())
    return "\n".join(ln for ln in lines if ln)


# Prefer a semantic main-content container over the whole body, so the returned text
# is the article rather than nav/sidebar/footer chrome. Tried in order; first with
# substantial text wins, else fall back to <body>.
_CONTENT_SELECTORS = ("main", "article", "[role=main]", "#content", "#mw-content-text")


async def _best_text(page: Any) -> str:
    """Readable text from the best content container on the page (body as fallback)."""
    for sel in _CONTENT_SELECTORS:
        try:
            el = await page.query_selector(sel)
        except Exception:  # noqa: BLE001 — bad selector / detached node; try the next
            continue
        if el:
            text = _readable(await el.inner_text())
            if len(text) >= 200:  # substantial — not just a header stub
                return text
    return _readable(await page.inner_text("body"))


@skill(
    name="browse_url",
    description=(
        "Load a web page (http/https) in a headless browser and return its title and "
        "readable text. Use to actually read a page's content — including JS-rendered "
        "sites — not just search snippets. Read-only: never clicks or submits forms."
    ),
    auto_confirm=True,
    timeout_s=45,
)
async def browse_url(url: str, max_chars: int = _MAX_CHARS) -> dict[str, Any]:
    """Fetch and read a web page.

    url: An http:// or https:// URL to load.
    max_chars: Truncate the returned text to this many characters (default 6000).
    """
    if not _http_url(url):
        return {"error": "only http(s) URLs are allowed", "url": url}
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"error": "playwright not installed — run: playwright install chromium", "url": url}

    max_chars = max(200, min(40_000, max_chars))
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True, args=["--disable-dev-shm-usage"]
            )
            try:
                page = await browser.new_page()
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                title = await page.title()
                text = await _best_text(page)
                status = resp.status if resp else None
                final_url = page.url
            finally:
                await browser.close()
    except Exception as e:  # noqa: BLE001 — surface a clean error, never raise into the agent loop
        return {"error": f"could not load page: {e}", "url": url}

    return {
        "url": final_url,
        "status": status,
        "title": title,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
        "length": len(text),
    }
