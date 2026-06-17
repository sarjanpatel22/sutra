"""Tests for the local RAG store/ingest/skill — the embedder is mocked (no Ollama)."""

from __future__ import annotations

import numpy as np
import pytest

from friday.rag import store as rag_store
from friday.rag.ingest import chunk_text
from friday.rag.store import Chunk, EmbedUnavailable, RagStore


def _vec(text: str) -> np.ndarray:
    """Deterministic 3-dim 'embedding' keyed on a topic word — makes cosine meaningful."""
    t = text.lower()
    v = np.zeros(3, dtype=np.float32)
    if "python" in t:
        v[0] = 1.0
    elif "weather" in t:
        v[1] = 1.0
    else:
        v[2] = 1.0
    return v


def _fake_embed(texts: list[str], kind: str = "document") -> np.ndarray:
    m = np.array([_vec(t) for t in texts], dtype=np.float32)
    return m / np.clip(np.linalg.norm(m, axis=1, keepdims=True), 1e-8, None)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(rag_store, "_RAG_DIR", tmp_path)
    monkeypatch.setattr(rag_store, "embed", _fake_embed)
    return RagStore("t")


def test_chunk_text_groups_paragraphs() -> None:
    text = "\n\n".join(f"para {i} " + "word " * 60 for i in range(5))
    chunks = chunk_text(text, target_words=120, overlap=10)
    assert len(chunks) >= 2  # 5*~61 words / 120 → multiple chunks
    assert all(c.strip() for c in chunks)


def test_chunk_text_empty() -> None:
    assert chunk_text("   \n\n  ") == []


def test_add_and_search_ranks_by_topic(store: RagStore) -> None:
    store.add([
        Chunk("python is a great language", "a.md"),
        Chunk("the weather is sunny today", "b.md"),
        Chunk("totally unrelated content here", "c.md"),
    ])
    assert len(store) == 3
    hits = store.search("tell me about python", k=2)
    assert hits[0]["source"] == "a.md"
    assert hits[0]["score"] >= hits[1]["score"]  # sorted descending


def test_add_dedups_repeated_chunks(store: RagStore) -> None:
    store.add([Chunk("python is great", "a.md")])
    # Re-ingesting the same chunk plus one new one: only the new one should land.
    added = store.add([Chunk("python is great", "a.md"), Chunk("the weather is nice", "b.md")])
    assert added == 1
    assert len(store) == 2


def test_search_empty_store_returns_empty(store: RagStore) -> None:
    assert store.search("anything") == []


def test_persistence_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(rag_store, "_RAG_DIR", tmp_path)
    monkeypatch.setattr(rag_store, "embed", _fake_embed)
    s1 = RagStore("c")
    s1.add([Chunk("python rocks", "x.md"), Chunk("weather report", "y.md")])
    s2 = RagStore("c")  # fresh instance loads from disk
    assert len(s2) == 2
    assert s2.search("python", k=1)[0]["source"] == "x.md"


async def test_rag_search_skill_happy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(rag_store, "_RAG_DIR", tmp_path)
    monkeypatch.setattr(rag_store, "embed", _fake_embed)
    from friday.skills.rag import rag_search

    RagStore("default").add([Chunk("python tips and tricks", "p.md")])
    out = await rag_search("python", k=3)
    assert out["n"] == 1
    assert out["hits"][0]["source"] == "p.md"


async def test_rag_search_skill_no_docs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(rag_store, "_RAG_DIR", tmp_path)
    from friday.skills.rag import rag_search

    out = await rag_search("anything", collection="empty")
    assert "error" in out and "ingest" in out["error"]


async def test_rag_search_skill_empty_query() -> None:
    from friday.skills.rag import rag_search

    assert "error" in await rag_search("   ")


async def test_rag_search_embed_unavailable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(rag_store, "_RAG_DIR", tmp_path)
    monkeypatch.setattr(rag_store, "embed", _fake_embed)
    from friday.skills.rag import rag_search

    RagStore("default").add([Chunk("python", "p.md")])

    def boom(texts, kind="document"):
        raise EmbedUnavailable("ollama down")

    monkeypatch.setattr(rag_store, "embed", boom)
    out = await rag_search("python")
    assert "error" in out and "unavailable" in out["error"]


# ── keep model warm (skip nomic's ~3s cold reload) ───────────────────────────
class _PostResp:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"embeddings": [[1.0, 0.0, 0.0]]}


def test_embed_payload_pins_model_keepalive(monkeypatch) -> None:
    # embed() must ask Ollama to keep nomic resident, or every query re-pays the load.
    captured: dict = {}

    def _fake_post(url, json, timeout):
        captured["payload"] = json
        return _PostResp()

    monkeypatch.delenv("RAG_EMBED_KEEPALIVE", raising=False)
    monkeypatch.setattr(rag_store.httpx, "post", _fake_post)
    rag_store.embed(["hello"], "query")
    assert captured["payload"]["keep_alive"] == -1  # default: never unload


def test_keepalive_env_accepts_seconds_or_duration(monkeypatch) -> None:
    monkeypatch.setenv("RAG_EMBED_KEEPALIVE", "1800")
    assert rag_store._keepalive() == 1800           # numeric → seconds
    monkeypatch.setenv("RAG_EMBED_KEEPALIVE", "30m")
    assert rag_store._keepalive() == "30m"          # duration string passes through
    monkeypatch.delenv("RAG_EMBED_KEEPALIVE", raising=False)
    assert rag_store._keepalive() == -1             # default


def test_warm_true_on_success_false_when_down(monkeypatch) -> None:
    def _ok(texts, kind="document"):
        return np.zeros((1, 3), np.float32)

    monkeypatch.setattr(rag_store, "embed", _ok)
    assert rag_store.warm() is True

    def _boom(texts, kind="document"):
        raise EmbedUnavailable("connection refused")

    monkeypatch.setattr(rag_store, "embed", _boom)
    assert rag_store.warm() is False
