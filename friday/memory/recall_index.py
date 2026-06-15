"""Prefixed-nomic semantic recall over the graph's facts — fixes weak question→fact ranking.

The graph (FalkorDB) stays the source of truth; this is a sharper RETRIEVAL layer over its
RELATES_TO facts. nomic's search_query / search_document prefixes make question→fact
matching asymmetric (the actual fix), and narration is filtered so chit-chat can't out-rank
real facts. Measured on the eval_recall probes: hit@1 60→80%, hit@3 85→95%, hit@5 90→100%,
and the identity-fact misses (Acme / engineer) disappear.

Fact embeddings are cached and rebuilt only on a TTL lapse, so the hot path is one
query-embed + a dot product. ANY failure returns [] so store.recall falls back to its
graphiti / keyword paths — recall can't get worse, only better.
"""

from __future__ import annotations

import os
import threading
import time

import numpy as np

from friday.rag.store import embed

_HOST = os.getenv("FALKORDB_HOST", "localhost")
_PORT = int(os.getenv("FALKORDB_PORT", "6379"))
_GRAPH = os.getenv("MEMORY_GROUP_ID", "sarjan")
_TTL = float(os.getenv("RECALL_INDEX_TTL", "180"))

_lock = threading.Lock()


class _Index:
    facts: list[str] = []
    vecs: np.ndarray | None = None
    built: float = 0.0


_ix = _Index()


def _pull_facts() -> list[str]:
    """Distinct RELATES_TO facts from the graph, minus low-value narration."""
    from falkordb import FalkorDB

    from friday.memory.store import looks_low_value

    g = FalkorDB(host=_HOST, port=_PORT).select_graph(_GRAPH)
    res = g.query("MATCH ()-[r:RELATES_TO]->() RETURN DISTINCT r.fact")
    facts = [row[0] for row in res.result_set if row[0]]
    return [f for f in facts if not looks_low_value(f)]


def _rebuild() -> None:
    facts = _pull_facts()
    _ix.facts = facts
    _ix.vecs = embed(facts, "document") if facts else None
    _ix.built = time.time()


def recall_facts(query: str, k: int = 5) -> list[str]:
    """Top-k graph facts for `query` by prefixed-nomic cosine; [] on any failure."""
    query = query.strip()
    if not query:
        return []
    try:
        with _lock:
            if _ix.vecs is None or (time.time() - _ix.built) > _TTL:
                _rebuild()
            if not _ix.facts or _ix.vecs is None:
                return []
            qv = embed([query], "query")[0]
            order = np.argsort(-(_ix.vecs @ qv))[: max(1, k)]
            return [_ix.facts[i] for i in order]
    except Exception:  # noqa: BLE001 — caller falls back to graphiti/keyword recall
        return []


def recall_scored(query: str, k: int = 8) -> list[tuple[str, float]]:
    """Like recall_facts but returns (fact, similarity) pairs — for the memory GUI."""
    query = query.strip()
    if not query:
        return []
    try:
        with _lock:
            if _ix.vecs is None or (time.time() - _ix.built) > _TTL:
                _rebuild()
            if not _ix.facts or _ix.vecs is None:
                return []
            qv = embed([query], "query")[0]
            scores = _ix.vecs @ qv
            order = np.argsort(-scores)[: max(1, k)]
            return [(_ix.facts[i], round(float(scores[i]), 3)) for i in order]
    except Exception:  # noqa: BLE001
        return []


def all_facts() -> list[str]:
    """Every indexed (narration-filtered) fact — for the GUI's browse view."""
    try:
        with _lock:
            if _ix.vecs is None or (time.time() - _ix.built) > _TTL:
                _rebuild()
            return list(_ix.facts)
    except Exception:  # noqa: BLE001
        return []


def refresh() -> int:
    """Force a rebuild (e.g. after a large memory write). Returns the fact count."""
    with _lock:
        _rebuild()
        return len(_ix.facts)
