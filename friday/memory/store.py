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
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_OLLAMA_BASE = os.getenv("GRAPHITI_LLM_BASE_URL", "http://localhost:11434/v1")
_LLM_API_KEY = os.getenv("GRAPHITI_LLM_API_KEY", "ollama")  # dummy for Ollama
_LLM_MODEL = os.getenv("GRAPHITI_LLM_MODEL", "gemma3:4b")
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
        driver = FalkorDriver(host=_FALKOR_HOST, port=_FALKOR_PORT)
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
        """Return fact strings relevant to query. Empty list if unavailable."""
        query = query.strip()
        if not query or not await self.initialize():
            return []
        try:
            edges = await self._graphiti.search(
                query, group_ids=[_GROUP_ID], num_results=limit
            )
            return [f for f in (getattr(e, "fact", None) for e in edges) if f]
        except Exception as e:  # noqa: BLE001
            logger.warning("memory.recall failed: %s", e)
            return []

    async def forget(self, query: str, limit: int = 10) -> int:
        """Remove episodes that produced facts matching query. Returns count removed."""
        query = query.strip()
        if not query or not await self.initialize():
            return 0
        try:
            edges = await self._graphiti.search(
                query, group_ids=[_GROUP_ID], num_results=limit
            )
            episode_uuids: set[str] = set()
            for e in edges:
                episode_uuids.update(getattr(e, "episodes", None) or [])
            for uid in episode_uuids:
                await self._graphiti.remove_episode(uid)
            return len(episode_uuids)
        except Exception as e:  # noqa: BLE001
            logger.warning("memory.forget failed: %s", e)
            return 0

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
