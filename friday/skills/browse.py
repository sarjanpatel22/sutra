"""Headless-browser web skills: read a page (browse_url) or pull structured data
(web_extract) with Playwright + Chromium.

Playwright drives a real headless browser, so these read JS-rendered pages a plain
HTTP fetch can't — the deterministic, token-cheap half of Phase 1.5 (no LLM in the
loop, unlike autonomous browser-use). Both are read-only: they load and read a page
you name, never click or submit, so they auto-run. browse_url returns the readable
article text; web_extract returns CSS-selector matches or a headings+links overview.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urljoin, urlparse

from friday.skills.registry import skill

_MAX_CHARS = 6000
_NAV_TIMEOUT_MS = 30_000

# Prefer a semantic main-content container over the whole body, so returned text is
# the article rather than nav/sidebar/footer chrome. First with substantial text wins.
_CONTENT_SELECTORS = ("main", "article", "[role=main]", "#content", "#mw-content-text")


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


async def _render(
    url: str, extract: Callable[[Any, Any], Awaitable[dict[str, Any]]]
) -> dict[str, Any]:
    """Validate the URL, drive headless chromium to it, run extract(page, response),
    and return its dict — or a clean error dict. Never raises into the agent loop."""
    if not _http_url(url):
        return {"error": "only http(s) URLs are allowed", "url": url}
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"error": "playwright not installed — run: playwright install chromium", "url": url}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            try:
                page = await browser.new_page()
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                return await extract(page, resp)
            finally:
                await browser.close()
    except Exception as e:  # noqa: BLE001 — surface a clean error, never raise into the loop
        return {"error": f"could not load page: {e}", "url": url}


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


async def _overview_links(page: Any, limit: int) -> list[dict[str, str]]:
    """Up to `limit` distinct on-page links as {text, href}, hrefs resolved absolute."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for el in await page.query_selector_all("a[href]"):
        href = await el.get_attribute("href") or ""
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        absu = urljoin(page.url, href)
        if absu in seen:
            continue
        seen.add(absu)
        text = " ".join((await el.inner_text()).split())[:120]
        out.append({"text": text, "href": absu})
        if len(out) >= limit:
            break
    return out


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
    chars = max(200, min(40_000, max_chars))

    async def _extract(page: Any, resp: Any) -> dict[str, Any]:
        title = await page.title()
        text = await _best_text(page)
        return {
            "url": page.url,
            "status": resp.status if resp else None,
            "title": title,
            "text": text[:chars],
            "truncated": len(text) > chars,
            "length": len(text),
        }

    return await _render(url, _extract)


@skill(
    name="web_extract",
    description=(
        "Extract structured data from a web page. With a CSS `selector`, returns the "
        "text (or a named `attr` like href/src) of each matching element — prices, "
        "table rows, list items, links. Without a selector, returns the page's "
        "headings and links as an overview. Read-only: never clicks or submits."
    ),
    auto_confirm=True,
    timeout_s=45,
)
async def web_extract(
    url: str, selector: str = "", attr: str = "", limit: int = 50
) -> dict[str, Any]:
    """Extract structured data from a web page.

    url: An http:// or https:// URL to load.
    selector: CSS selector to match (e.g. "h2", ".price", "table tr"). Empty = overview.
    attr: Element attribute to return instead of text (e.g. "href", "src"). Empty = text.
    limit: Maximum number of items to return (1-200, default 50).
    """
    sel = selector.strip()
    at = attr.strip()
    n = max(1, min(200, limit))

    async def _extract(page: Any, resp: Any) -> dict[str, Any]:
        if sel:
            try:
                els = await page.query_selector_all(sel)
            except Exception as e:  # noqa: BLE001 — invalid CSS selector
                return {"error": f"invalid selector: {e}", "url": page.url, "selector": sel}
            items: list[str] = []
            for el in els[:n]:
                if at:
                    val = await el.get_attribute(at)
                else:
                    val = " ".join((await el.inner_text()).split())
                if val and val.strip():
                    items.append(val.strip())
            return {
                "url": page.url,
                "selector": sel,
                "attr": at or "text",
                "count": len(items),
                "items": items,
            }
        # No selector → structured overview (headings + links).
        headings: list[str] = []
        for el in (await page.query_selector_all("h1, h2, h3"))[:n]:
            t = " ".join((await el.inner_text()).split())
            if t:
                headings.append(t)
        return {
            "url": page.url,
            "title": await page.title(),
            "headings": headings,
            "links": await _overview_links(page, n),
        }

    return await _render(url, _extract)
