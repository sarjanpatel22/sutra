"""Web search via self-hosted SearXNG."""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from friday.skills.registry import skill

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8080")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


@skill(
    name="web_search",
    description="Search the web for CURRENT or external facts (news, prices, sports scores, "
    "recent events, looking something up). NOT for general knowledge you already know or "
    "opinion/philosophy questions.",
    auto_confirm=True,
    timeout_s=10,
)
async def web_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Search the web.

    query: Search query string.
    max_results: Maximum number of results to return (1-20).
    """
    max_results = max(1, min(20, max_results))
    params = {
        "q": query,
        "format": "json",
        "categories": "general",
        "language": "en",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{SEARXNG_URL}/search", params=params)
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("results", [])[:max_results]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
        })
    return results
