"""Memory store + skills tests — focus on graceful degradation when DB is down."""

import pytest

from friday.memory.store import MemoryStore
from friday.skills import registry


@pytest.fixture(autouse=True)
def discover_skills():
    registry.autodiscover()


def test_memory_skills_registered():
    skills = registry.list_skills()
    assert "recall" in skills
    assert "forget" in skills
    # forget deletes memories — must require confirmation
    assert registry.get("forget").auto_confirm is False
    assert registry.get("recall").auto_confirm is True


@pytest.mark.asyncio
async def test_initialize_unavailable_is_graceful(monkeypatch):
    """With FalkorDB unreachable, initialize() returns False, never raises."""
    store = MemoryStore()

    def boom():
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(store, "_build", boom)
    assert await store.initialize() is False
    # cached: a second call must not retry the build
    assert await store.initialize() is False


@pytest.mark.asyncio
async def test_disabled_via_env(monkeypatch):
    monkeypatch.setattr("friday.memory.store._ENABLED", False)
    store = MemoryStore()
    assert await store.initialize() is False


@pytest.mark.asyncio
async def test_recall_returns_empty_when_unavailable(monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr(store, "_build", lambda: (_ for _ in ()).throw(ConnectionError()))
    assert await store.recall("anything") == []


@pytest.mark.asyncio
async def test_remember_returns_false_when_unavailable(monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr(store, "_build", lambda: (_ for _ in ()).throw(ConnectionError()))
    assert await store.remember("a fact") is False


@pytest.mark.asyncio
async def test_remember_empty_text_is_false():
    store = MemoryStore()
    assert await store.remember("   ") is False


@pytest.mark.asyncio
async def test_recall_formats_facts(monkeypatch):
    """recall() pulls .fact off edges and drops empties."""
    store = MemoryStore()
    store._initialized = True
    store._available = True

    class Edge:
        def __init__(self, fact):
            self.fact = fact

    class FakeGraphiti:
        async def search(self, query, group_ids=None, num_results=5):
            return [Edge("Sarjan works at Acme Corp"), Edge(""), Edge("Sutra is voice-native")]

    store._graphiti = FakeGraphiti()
    facts = await store.recall("sarjan")
    assert facts == ["Sarjan works at Acme Corp", "Sutra is voice-native"]
