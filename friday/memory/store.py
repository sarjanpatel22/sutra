"""Graph memory store: Graphiti over FalkorDB, all-free local backends.

Entity extraction (LLM) and embeddings both run through Ollama's OpenAI-compatible
endpoint by default — gemma3:4b + nomic-embed-text — so memory works with zero API
keys and no internet. Point GRAPHITI_LLM_* at Groq/OpenAI later if you want sharper
extraction.

The store degrades gracefully: if FalkorDB is unreachable, recall() returns [] and
remember() returns False instead of raising, so the agent keeps working without
persistent memory. Connection is attempted once and cached.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Tiny stopword set to keep keyword recall focused on content terms. RediSearch
# drops its own stopwords too; this just trims obvious query filler.
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "did", "do", "does",
    "for", "from", "how", "in", "is", "it", "of", "on", "or", "the", "to", "was",
    "we", "what", "when", "where", "which", "who", "why", "with", "you", "your",
    "i", "me", "my", "our", "us", "that", "this", "have", "has", "use", "using",
}


def _keywords(query: str, limit: int = 10) -> list[str]:
    """Content words from a query, lowercased, for a RediSearch OR match."""
    toks = re.findall(r"[a-z0-9]+", query.lower())
    return [t for t in toks if len(t) > 1 and t not in _STOPWORDS][:limit]


def looks_like_noise(text: str) -> bool:
    """True if text reads like STT garbage rather than a real utterance.

    Targets the junk a far-field / echoing mic transcribes — single-letter
    spell-outs ("M-H-H-H-H"), runs of one repeated character ("Hhhhh"), or strings
    with no real words — so it never reaches long-term memory. Deliberately
    conservative: typed text and normal short commands ("yes", "stop it") pass.
    Plausible-but-wrong words (e.g. "reitutra") are caught upstream by STT
    confidence, not here.
    """
    t = text.strip()
    if not t:
        return True
    words = re.findall(r"[a-z]+", t.lower())
    if not words:
        return True  # all punctuation / digits / symbols
    # One character repeated 4+ times in a row (ignoring spaces/hyphens): "Hhhhh".
    if re.search(r"(.)\1{3,}", re.sub(r"[\s\-]", "", t)):
        return True
    # Single-letter "words" dominate -> spell-out garbage like "M H H H".
    singles = sum(1 for w in words if len(w) == 1)
    return singles >= 3 and singles / len(words) >= 0.5

_OLLAMA_BASE = os.getenv("GRAPHITI_LLM_BASE_URL", "http://localhost:11434/v1")
_LLM_API_KEY = os.getenv("GRAPHITI_LLM_API_KEY", "ollama")  # dummy for Ollama
# qwen2.5:14b for extraction — gemma3:4b drops relationships, qwen2.5:7b leaks
# Chinese; 14b extracts cleanly (see Phase 1.3 notes). gemma stays the voice handler.
_LLM_MODEL = os.getenv("GRAPHITI_LLM_MODEL", "qwen2.5:14b")
_EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
_EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))
_FALKOR_HOST = os.getenv("FALKORDB_HOST", "localhost")
_FALKOR_PORT = int(os.getenv("FALKORDB_PORT", "6379"))
_GROUP_ID = os.getenv("MEMORY_GROUP_ID", "sarjan")  # single-user namespace
_ENABLED = os.getenv("MEMORY_ENABLED", "1").lower() not in ("0", "false", "no")


class MemoryStore:
    """Async wrapper around a Graphiti graph keyed to one user namespace."""

    def __init__(self) -> None:
        self._graphiti: Any = None
        self._available = False
        self._initialized = False

    def _build(self) -> Any:
        """Construct a Graphiti instance wired to all-free local clients.

        FalkorDriver connects eagerly, so this can raise ConnectionError when
        the DB is down — callers handle that.
        """
        from graphiti_core import Graphiti
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.driver.falkordb_driver import FalkorDriver
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

        llm = OpenAIGenericClient(
            config=LLMConfig(
                api_key=_LLM_API_KEY,
                model=_LLM_MODEL,
                small_model=_LLM_MODEL,
                base_url=_OLLAMA_BASE,
            )
        )
        embedder = OpenAIEmbedder(
            config=OpenAIEmbedderConfig(
                embedding_model=_EMBED_MODEL,
                embedding_dim=_EMBED_DIM,
                api_key=_LLM_API_KEY,
                base_url=_OLLAMA_BASE,
            )
        )
        # Reuse the local LLM as reranker so no OpenAI key is ever required.
        reranker = OpenAIRerankerClient(
            config=LLMConfig(api_key=_LLM_API_KEY, model=_LLM_MODEL, base_url=_OLLAMA_BASE),
            client=llm,
        )
        # database MUST be the group_id: Graphiti routes WRITES (add_episode) to a
        # graph named after the group_id, but its search execute_query reads from
        # driver._database (default "default_db"). Left unset, writes land in
        # `sarjan` while search reads an empty `default_db` -> recall always 0.
        driver = FalkorDriver(host=_FALKOR_HOST, port=_FALKOR_PORT, database=_GROUP_ID)
        return Graphiti(
            graph_driver=driver,
            llm_client=llm,
            embedder=embedder,
            cross_encoder=reranker,
        )

    async def initialize(self) -> bool:
        """Connect and build indices once. Returns True if memory is usable."""
        if self._initialized:
            return self._available
        self._initialized = True
        if not _ENABLED:
            logger.info("memory: disabled via MEMORY_ENABLED")
            return False
        try:
            self._graphiti = self._build()
            await self._graphiti.build_indices_and_constraints()
            self._available = True
            logger.info("memory: connected to FalkorDB at %s:%s", _FALKOR_HOST, _FALKOR_PORT)
        except Exception as e:  # noqa: BLE001 — DB down / misconfig; run without memory
            self._available = False
            logger.warning("memory: unavailable (%s) — running without persistent memory", e)
        return self._available

    async def remember(
        self,
        text: str,
        *,
        source: str = "conversation",
        reference_time: datetime | None = None,
    ) -> bool:
        """Persist an episode. Returns False (no raise) if memory is unavailable."""
        text = text.strip()
        if not text or not await self.initialize():
            return False
        try:
            now = datetime.now(UTC)
            await self._graphiti.add_episode(
                name=f"{source}-{now.isoformat()}",
                episode_body=text,
                source_description=source,
                reference_time=reference_time or now,
                group_id=_GROUP_ID,
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("memory.remember failed: %s", e)
            return False

    async def recall(self, query: str, limit: int = 5) -> list[str]:
        """Return fact strings relevant to query. Empty list if unavailable.

        Tries Graphiti's hybrid (semantic + BM25) search first, then falls back to
        a direct full-text query on the graph. The fallback exists because
        Graphiti's .search() returns empty against FalkorDB even when the index
        holds the data (verified directly) — without it, recall is silently dead.
        """
        query = query.strip()
        if not query or not await self.initialize():
            return []
        try:
            edges = await self._graphiti.search(
                query, group_ids=[_GROUP_ID], num_results=limit
            )
            facts = [f for f in (getattr(e, "fact", None) for e in edges) if f]
            if facts:
                return facts
        except Exception as e:  # noqa: BLE001
            logger.warning("memory.recall graphiti search failed: %s", e)
        return self._fulltext_recall(query, limit)

    def _fulltext_recall(self, query: str, limit: int) -> list[str]:
        """Deterministic, LLM-free keyword recall straight off the RELATES_TO index."""
        terms = _keywords(query)
        if not terms:
            return []
        try:
            from falkordb import FalkorDB

            g = FalkorDB(host=_FALKOR_HOST, port=_FALKOR_PORT).select_graph(_GROUP_ID)
            res = g.query(
                "CALL db.idx.fulltext.queryRelationships('RELATES_TO', $q) "
                "YIELD relationship, score "
                "WHERE relationship.group_id = $gid "
                "RETURN relationship.fact AS fact ORDER BY score DESC LIMIT $limit",
                {"q": " | ".join(terms), "gid": _GROUP_ID, "limit": limit},
            )
            seen: set[str] = set()
            out: list[str] = []
            for row in res.result_set:
                fact = row[0]
                if fact and fact not in seen:
                    seen.add(fact)
                    out.append(fact)
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("memory.recall fulltext fallback failed: %s", e)
            return []

    async def forget(
        self, query: str, limit: int = 10, *, apply: bool = False
    ) -> dict[str, Any]:
        """Find — and only on apply=True, delete — episodes matching query.

        forget is destructive and now matches by *meaning* (semantic search), so a
        loose query can target unrelated facts. To prevent that, this previews by
        default: with apply=False it deletes NOTHING and returns the facts that
        WOULD be removed; the caller must look, then re-call with apply=True.
        Deleting an episode drops every fact derived from it (preview shows them).

        Returns {"matched": [fact, ...], "episodes": [uuid, ...], "removed": int}.
        """
        result: dict[str, Any] = {"matched": [], "episodes": [], "removed": 0}
        query = query.strip()
        if not query or not await self.initialize():
            return result
        try:
            edges = await self._graphiti.search(
                query, group_ids=[_GROUP_ID], num_results=limit
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("memory.forget search failed: %s", e)
            return result

        episode_uuids: list[str] = []
        seen: set[str] = set()
        for e in edges:
            fact = getattr(e, "fact", None)
            if fact:
                result["matched"].append(fact)
            for uid in getattr(e, "episodes", None) or []:
                if uid not in seen:
                    seen.add(uid)
                    episode_uuids.append(uid)
        result["episodes"] = episode_uuids

        if not apply:  # preview only — never delete implicitly
            return result

        removed = 0
        for uid in episode_uuids:
            try:
                await self._graphiti.remove_episode(uid)
                removed += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("memory.forget remove_episode %s failed: %s", uid, e)
        result["removed"] = removed
        return result

    def list_episodes(self) -> list[dict[str, str]]:
        """Every episode (uuid, name, content) via the sync client — for offline tools.

        The graph is namespaced by database=group_id, so no group filter is needed.
        Returns [] if FalkorDB is unreachable.
        """
        try:
            from falkordb import FalkorDB

            g = FalkorDB(host=_FALKOR_HOST, port=_FALKOR_PORT).select_graph(_GROUP_ID)
            res = g.query("MATCH (e:Episodic) RETURN e.uuid, e.name, e.content")
            return [
                {"uuid": r[0] or "", "name": r[1] or "", "content": r[2] or ""}
                for r in res.result_set
            ]
        except Exception as e:  # noqa: BLE001
            logger.warning("memory.list_episodes failed: %s", e)
            return []

    async def remove_episode(self, uuid: str) -> bool:
        """Delete one episode and the facts derived solely from it. False if unavailable."""
        if not uuid or not await self.initialize():
            return False
        try:
            await self._graphiti.remove_episode(uuid)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("memory.remove_episode %s failed: %s", uuid, e)
            return False

    def reset(self) -> None:
        """Drop the cached connection so the next initialize() reconnects.

        initialize() caches availability for the whole process — fine for the CLI,
        but a long-running daemon would log-but-never-persist if FalkorDB restarted.
        Calling reset() after a failed write lets memory recover on the next attempt.
        """
        self._initialized = False
        self._available = False
        self._graphiti = None

    async def close(self) -> None:
        if self._graphiti is not None:
            try:
                await self._graphiti.close()
            except Exception:  # noqa: BLE001
                pass


_instance: MemoryStore | None = None


def get_store() -> MemoryStore:
    global _instance
    if _instance is None:
        _instance = MemoryStore()
    return _instance
