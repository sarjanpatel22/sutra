"""Web scraping skill — pull structured data off a web page or JSON API.

Tiered, cheapest-that-works: plain httpx (a real browser User-Agent + retry/backoff)
for the fetch, then BeautifulSoup for structured extraction (tables, CSS selects, links)
when it's installed, with a stdlib fallback for text/links when it isn't. Cloudflare
("Just a moment…") pages escalate to cloudscraper. The powerful extraction (tables,
CSS select, Cloudflare) needs the optional extra:  pip install -e '.[scrape]'.

Complements http_request (raw API calls) and browser_agent (full JS rendering); this is
the static-HTML / structured-extraction workhorse Sutra was missing.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import re
from typing import Any
from urllib.parse import urljoin

import httpx

from friday.skills.registry import skill

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

try:  # BeautifulSoup is optional — the skill degrades without it
    import bs4  # noqa: F401

    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False


def _soup(markup: str) -> Any:
    from bs4 import BeautifulSoup

    try:
        return BeautifulSoup(markup, "lxml")
    except Exception:  # noqa: BLE001 — lxml not built; fall back to the stdlib parser
        return BeautifulSoup(markup, "html.parser")


def _text_stdlib(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", markup)
    text = _html.unescape(re.sub(r"(?s)<[^>]+>", " ", markup))
    text = re.sub(r"[ \t\r\f]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _links_stdlib(markup: str, base: str) -> list[str]:
    return [urljoin(base, h) for h in re.findall(r'href=["\']([^"\']+)["\']', markup)]


async def _cloudscraper_get(url: str) -> tuple[str | None, int | None, str | None]:
    try:
        import cloudscraper  # noqa: F401
    except ImportError:
        return None, None, "cloudscraper not installed; add it with: pip install -e '.[scrape]'"

    def _run() -> tuple[str, int]:
        import cloudscraper

        s = cloudscraper.create_scraper()
        resp = s.get(url, timeout=30)
        return resp.text, resp.status_code

    try:
        text, status = await asyncio.to_thread(_run)
        return text, status, None
    except Exception as e:  # noqa: BLE001
        return None, None, f"cloudscraper failed: {e}"


_BLOCK_STATUS = (403, 429, 503)


def _block_hint(status: int | None) -> str | None:
    """A WAF/JS-challenge status that even cloudscraper won't clear — point at browser_agent."""
    if status in _BLOCK_STATUS:
        return (
            f"HTTP {status}: looks like a WAF / JS challenge (e.g. Cloudflare Turnstile or a "
            "managed challenge). The cloudscraper tier only clears the older JS-math challenge, "
            "not modern ones — use the browser_agent skill (a real browser) to render this URL."
        )
    return None


async def _get_html(url: str, cloudflare: bool) -> tuple[str | None, int | None, str | None, str]:
    """Return (html, status, error, via). httpx first, cloudscraper on block/force."""
    err: str | None = None
    if not cloudflare:
        async with httpx.AsyncClient(
            timeout=30, headers=_HEADERS, follow_redirects=True
        ) as c:
            for attempt in range(3):
                try:
                    r = await c.get(url)
                    if r.status_code not in (403, 429):
                        return r.text, r.status_code, None, "httpx"
                    err = f"HTTP {r.status_code} (blocked)"
                    break  # a WAF block — let cloudscraper try
                except Exception as e:  # noqa: BLE001
                    err = str(e)
                    await asyncio.sleep(0.4 * (attempt + 1))
    text, status, cf_err = await _cloudscraper_get(url)
    if text is not None:
        return text, status, None, "cloudscraper"
    return None, status, cf_err or err, "cloudscraper" if cloudflare else "httpx"


@skill(
    name="web_scrape",
    description=(
        "Scrape and extract data from a web page or JSON API: readable text, every HTML "
        "table, all links, or a CSS selector. Handles static HTML and auto-escalates to "
        "cloudscraper for Cloudflare-blocked pages. Use to pull a table/list/directory, "
        "gather contacts or prices, read what's on a URL, or hit a JSON endpoint. For a "
        "single quick read use web_search/http_request; for JS-rendered SPAs use "
        "browser_agent. modes: text | tables | links | select | html | json."
    ),
    auto_confirm=True,
    timeout_s=45,
)
async def web_scrape(
    url: str,
    mode: str = "text",
    select: str = "",
    cloudflare: bool = False,
    limit: int = 50,
    max_chars: int = 20000,
) -> dict[str, Any]:
    """Fetch a URL and extract data from it.

    url: the page or API endpoint (scheme optional; https assumed).
    mode: text (readable text) | tables (every <table>) | links (absolute links) |
          select (CSS selector) | html (raw HTML) | json (parse the body as JSON).
    select: CSS selector, used when mode="select" (e.g. ".product .price").
    cloudflare: force the cloudscraper path for a Cloudflare-protected page.
    limit: cap the number of links/tables/select-results returned.
    max_chars: cap the length of returned text/html.
    """
    url = (url or "").strip()
    if not url:
        return {"error": "empty url"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    mode = (mode or "text").lower().strip()
    try:
        limit = max(1, int(limit))
        max_chars = max(100, int(max_chars))
    except (TypeError, ValueError):
        return {"error": "limit and max_chars must be numbers"}
    if mode in ("tables", "select") and not _HAS_BS4:
        return {
            "error": f"mode '{mode}' needs BeautifulSoup; install it with: "
            f"pip install -e '.[scrape]'",
            "url": url,
        }

    html, status, err, via = await _get_html(url, cloudflare)
    if html is None:
        return {"error": f"fetch failed: {err}", "url": url, "via": via}

    base = {"url": url, "status": status, "via": via, "mode": mode}
    hint = _block_hint(status)
    if hint:  # even the fallback couldn't clear it — say so instead of returning a block page
        base["blocked"] = True
        base["hint"] = hint

    if mode == "json":
        try:
            data = json.loads(html)
        except ValueError:
            return {**base, "error": "response body is not valid JSON"}
        return {**base, "data": data}

    if mode == "html":
        return {**base, "chars": len(html), "html": html[:max_chars]}

    if mode == "text":
        text = _soup(html).get_text("\n", strip=True) if _HAS_BS4 else _text_stdlib(html)
        if _HAS_BS4:  # drop boilerplate when we can
            text = re.sub(r"\n\s*\n+", "\n\n", text)
        return {**base, "chars": len(text), "truncated": len(text) > max_chars,
                "text": text[:max_chars]}

    if mode == "links":
        if _HAS_BS4:
            soup = _soup(html)
            raw = [urljoin(url, a["href"]) for a in soup.find_all("a", href=True)]
        else:
            raw = _links_stdlib(html, url)
        seen, links = set(), []
        for href in raw:
            if href not in seen and href.startswith(("http://", "https://")):
                seen.add(href)
                links.append(href)
        return {**base, "count": len(links), "links": links[:limit]}

    if mode == "tables":
        tables = []
        for tbl in _soup(html).find_all("table")[:limit]:
            rows = []
            for tr in tbl.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                if cells:
                    rows.append(cells)
            if rows:
                tables.append(rows[:500])
        return {**base, "table_count": len(tables), "tables": tables}

    if mode == "select":
        if not select:
            return {**base, "error": "mode='select' needs a CSS selector in `select`"}
        out = []
        for el in _soup(html).select(select)[:limit]:
            out.append({
                "text": el.get_text(" ", strip=True),
                "href": urljoin(url, el["href"]) if el.has_attr("href") else None,
            })
        return {**base, "selector": select, "count": len(out), "results": out}

    return {**base, "error": f"unknown mode '{mode}'",
            "valid_modes": ["text", "tables", "links", "select", "html", "json"]}
