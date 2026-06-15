"""rag_search — pull relevant passages from the user's ingested documents to ground answers.

Read-only retrieval: it returns the user's own document chunks so Sutra can answer from
their files instead of guessing. Ingest with `sutra rag ingest <path>` first.
"""

from __future__ import annotations

import asyncio
from typing import Any

from friday.rag.store import EmbedUnavailable, RagStore
from friday.skills.registry import skill


@skill(
    name="rag_search",
    description=(
        "Search the user's OWN ingested documents/notes/code for passages relevant to a "
        "question, and return them so you can answer grounded in their files (with sources). "
        "Use for questions about the user's docs, project, notes, or codebase. Read-only."
    ),
    auto_confirm=True,
    timeout_s=30,
)
async def rag_search(query: str, k: int = 5, collection: str = "default") -> dict[str, Any]:
    """Return the top-k most relevant document chunks for `query`."""
    query = query.strip()
    if not query:
        return {"error": "empty query"}
    store = RagStore(collection)
    if len(store) == 0:
        return {"error": f"no documents ingested in '{collection}' — run 'sutra rag ingest <path>'"}
    try:
        hits = await asyncio.to_thread(store.search, query, k)
    except EmbedUnavailable as e:
        return {"error": f"embedding model unavailable: {e}"}
    return {"query": query, "n": len(hits), "hits": hits}
