"""Conversation history that survives restarts — Redis-backed, in-memory fallback.

Persists the chat transcript so `sutra chat` resumes where it left off. If Redis is
down (it's optional), it degrades to an in-process buffer — same graceful pattern as
the memory store: never raise, just lose persistence. Trims to the last `max_turns`
messages so the list (and the model context) stays bounded.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("sutra.history")


class History:
    """Append-only chat history for one session, persisted to Redis when available."""

    def __init__(
        self,
        session: str = "default",
        *,
        max_turns: int = 100,
        url: str | None = None,
    ) -> None:
        self._key = f"sutra:history:{session}"
        self._max = max_turns
        self._mem: list[dict[str, Any]] = []
        self._redis = self._connect(url or os.getenv("REDIS_URL") or "redis://localhost:6380")

    @staticmethod
    def _connect(url: str) -> Any:
        try:
            import redis

            client = redis.from_url(url, socket_connect_timeout=0.5, decode_responses=True)
            client.ping()
            return client
        except Exception as e:  # noqa: BLE001 — Redis optional; degrade to memory
            logger.info("history: Redis unavailable (%s) — in-memory only", e)
            return None

    @property
    def persistent(self) -> bool:
        return self._redis is not None

    def load(self) -> list[dict[str, Any]]:
        """Return the stored history (most recent `max_turns` messages)."""
        if self._redis is not None:
            try:
                raw = self._redis.lrange(self._key, 0, -1)
                return [json.loads(x) for x in raw]
            except Exception as e:  # noqa: BLE001
                logger.warning("history load failed: %s", e)
        return list(self._mem)

    def append(self, role: str, content: str) -> None:
        msg = {"role": role, "content": content}
        if self._redis is not None:
            try:
                self._redis.rpush(self._key, json.dumps(msg))
                self._redis.ltrim(self._key, -self._max, -1)
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("history append failed: %s", e)
        self._mem.append(msg)
        if len(self._mem) > self._max:
            self._mem = self._mem[-self._max :]

    def append_turn(self, user: str, assistant: str) -> None:
        self.append("user", user)
        self.append("assistant", assistant)

    def clear(self) -> None:
        self._mem = []
        if self._redis is not None:
            try:
                self._redis.delete(self._key)
            except Exception as e:  # noqa: BLE001
                logger.warning("history clear failed: %s", e)
