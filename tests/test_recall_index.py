"""Tests for the prefixed-nomic recall index — embedder + FalkorDB are mocked."""

from __future__ import annotations

import numpy as np
import pytest

from friday.memory import recall_index as ri


def _vec(text: str) -> np.ndarray:
    """Topic-keyed fake embedding so cosine ranking is meaningful in tests."""
    t = text.lower()
    v = np.zeros(3, dtype=np.float32)
    if any(w in t for w in ("scale", "work", "engineer", "employer")):
        v[0] = 1.0
    elif any(w in t for w in ("toronto", "live", "from")):
        v[1] = 1.0
    else:
        v[2] = 1.0
    return v


def _fake_embed(texts: list[str], kind: str = "document") -> np.ndarray:
    m = np.array([_vec(t) for t in texts], dtype=np.float32)
    return m / np.clip(np.linalg.norm(m, axis=1, keepdims=True), 1e-8, None)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(ri, "embed", _fake_embed)
    ri._ix.facts = []
    ri._ix.vecs = None
    ri._ix.built = 0.0
    yield


def test_ranks_right_fact_first(monkeypatch) -> None:
    monkeypatch.setattr(ri, "_pull_facts", lambda: [
        "Sarjan is a software engineer at Acme Corp",
        "Sarjan lives in Toronto",
        "some unrelated note about the weather",
    ])
    out = ri.recall_facts("who does Sarjan work for", k=2)
    assert "Acme" in out[0]


def test_empty_query_returns_empty() -> None:
    assert ri.recall_facts("   ") == []


def test_pull_failure_returns_empty(monkeypatch) -> None:
    def boom() -> list[str]:
        raise RuntimeError("falkordb down")

    monkeypatch.setattr(ri, "_pull_facts", boom)
    assert ri.recall_facts("anything") == []  # caller falls back to graphiti


def test_no_facts_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr(ri, "_pull_facts", lambda: [])
    assert ri.recall_facts("anything") == []


def test_cache_avoids_rebuild(monkeypatch) -> None:
    calls = {"n": 0}

    def pull() -> list[str]:
        calls["n"] += 1
        return ["Sarjan works at Acme Corp as an engineer"]

    monkeypatch.setattr(ri, "_pull_facts", pull)
    ri.recall_facts("work", k=1)
    ri.recall_facts("work", k=1)  # within TTL → served from cache
    assert calls["n"] == 1


def test_stale_triggers_rebuild(monkeypatch) -> None:
    monkeypatch.setattr(ri, "_pull_facts", lambda: ["Sarjan works at Acme Corp engineer"])
    monkeypatch.setattr(ri, "_TTL", -1.0)  # everything is immediately stale
    calls = {"n": 0}
    orig = ri._rebuild

    def counting() -> None:
        calls["n"] += 1
        orig()

    monkeypatch.setattr(ri, "_rebuild", counting)
    ri.recall_facts("work", k=1)
    ri.recall_facts("work", k=1)
    assert calls["n"] == 2  # rebuilt each time because TTL is negative


def test_refresh_returns_count(monkeypatch) -> None:
    monkeypatch.setattr(ri, "_pull_facts", lambda: ["an Acme fact", "a Toronto fact"])
    assert ri.refresh() == 2
