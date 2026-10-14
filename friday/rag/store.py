"""Lightweight local RAG vector store — $0, no extra infrastructure.

Embeds with nomic-embed-text via Ollama (already pulled), stores chunks + vectors on
disk, retrieves by cosine. No Qdrant/Docker: a normalized matrix + a dot product is
plenty for a personal doc corpus and matches the project's stdlib-lean bias. nomic's
asymmetric task prefixes (search_document / search_query) are applied because they
measurably sharpen retrieval.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import numpy as np

_OLLAMA = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
_EMBED_MODEL = os.getenv("RAG_EMBED_MODEL", os.getenv("EMBED_MODEL", "nomic-embed-text"))
_RAG_DIR = Path(os.getenv("RAG_DIR", str(Path.home() / ".local" / "share" / "friday" / "rag")))

_log = logging.getLogger(__name__)


class EmbedUnavailable(RuntimeError):
    """Ollama embedding endpoint unreachable — retrieval can't run."""


def _keepalive() -> int | str:
    """How long Ollama keeps the embedding model resident after a call. Default -1 =
    never unload, so a query skips nomic's ~3s cold reload every time (the model is
    ~275MB). Set RAG_EMBED_KEEPALIVE to a seconds int or a duration like "30m" to bound
    it. Memory recall reuses embed() too, so this warms RAG *and* recall."""
    v = os.getenv("RAG_EMBED_KEEPALIVE", "-1").strip()
    try:
        return int(v)
    except ValueError:
        if re.fullmatch(r"\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h)", v):
            return v  # a valid Ollama duration like "30m" / "90s"
        _log.warning("RAG_EMBED_KEEPALIVE=%r is not an int or a duration like '30m'; using -1", v)
        return -1


def embed(texts: list[str], kind: str = "document") -> np.ndarray:
    """Embed texts via Ollama nomic-embed-text → L2-normalized (n, dim) float32 matrix.

    kind="query" for a search query, "document" for stored chunks (nomic prefixes).
    """
    prefix = "search_query: " if kind == "query" else "search_document: "
    payload = {"model": _EMBED_MODEL, "input": [prefix + t for t in texts],
               "keep_alive": _keepalive()}
    try:
        r = httpx.post(f"{_OLLAMA}/api/embed", json=payload, timeout=120)
        r.raise_for_status()
        embs = r.json().get("embeddings")
    except Exception as e:  # noqa: BLE001 — surface as a typed, catchable failure
        raise EmbedUnavailable(str(e)) from e
    if not embs:
        raise EmbedUnavailable("no embeddings returned")
    m = np.asarray(embs, dtype=np.float32)
    normed: np.ndarray = m / np.clip(np.linalg.norm(m, axis=1, keepdims=True), 1e-8, None)
    return normed


def warm() -> bool:
    """Pre-load and pin the embedding model so the first real query skips nomic's ~3s
    cold load. Best-effort — returns False on ANY failure (Ollama down, a bad response),
    so it is safe to fire from a startup background thread without a stray traceback."""
    try:
        embed(["warm"], "query")
        return True
    except Exception:  # noqa: BLE001 — best-effort warm; must never raise from a bg thread
        return False


@dataclass
class Chunk:
    text: str
    source: str
    meta: dict[str, Any] = field(default_factory=dict)


class RagStore:
    """A named collection of chunks + their vectors, persisted under RAG_DIR."""

    def __init__(self, collection: str = "default") -> None:
        self.collection = collection
        self.dir = _RAG_DIR / collection
        self.chunks: list[Chunk] = []
        self.vecs: np.ndarray = np.zeros((0, 0), dtype=np.float32)
        self._load()

    def _load(self) -> None:
        cj, vn = self.dir / "chunks.jsonl", self.dir / "vectors.npy"
        if cj.exists() and vn.exists():
            self.chunks = [Chunk(**json.loads(line))
                           for line in cj.read_text().splitlines() if line.strip()]
            self.vecs = np.load(vn)

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "chunks.jsonl", "w") as f:
            for c in self.chunks:
                f.write(json.dumps({"text": c.text, "source": c.source, "meta": c.meta}) + "\n")
        np.save(self.dir / "vectors.npy", self.vecs)

    def add(self, chunks: list[Chunk]) -> int:
        """Embed and append chunks; persist. Returns the number added.

        Skips chunks whose exact text is already stored (and de-dups within the
        incoming batch), so re-ingesting the same document doesn't pile up duplicate
        chunks that then dominate retrieval. Only the fresh chunks are embedded.
        """
        if not chunks:
            return 0
        existing = {c.text for c in self.chunks}
        fresh: list[Chunk] = []
        for c in chunks:
            if c.text in existing:
                continue
            existing.add(c.text)  # also collapses duplicates inside this batch
            fresh.append(c)
        if not fresh:
            return 0
        new = embed([c.text for c in fresh], "document")
        self.vecs = new if self.vecs.size == 0 else np.vstack([self.vecs, new])
        self.chunks.extend(fresh)
        self._save()
        return len(fresh)

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        """Top-k chunks by cosine similarity to the query."""
        if not self.chunks:
            return []
        q = embed([query], "query")[0]
        scores = self.vecs @ q
        order = np.argsort(-scores)[: max(1, k)]
        return [{"score": round(float(scores[i]), 4), "text": self.chunks[i].text,
                 "source": self.chunks[i].source} for i in order]

    def __len__(self) -> int:
        return len(self.chunks)
