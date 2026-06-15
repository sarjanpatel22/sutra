"""Memory store + skills tests — focus on graceful degradation when DB is down."""

import asyncio

import pytest

from friday.memory.store import MemoryStore, _keywords
from friday.skills import registry


def test_keywords_strips_stopwords_and_punctuation():
    # "where does Sarjan work" -> content words only, lowercased
    assert _keywords("Where does Sarjan work?") == ["sarjan", "work"]


def test_keywords_splits_on_punctuation():
    # small.en -> two tokens (RediSearch tokenizes on the dot too)
    assert _keywords("the small.en Whisper model") == ["small", "en", "whisper", "model"]


def test_keywords_empty_for_all_stopwords():
    assert _keywords("what is it for") == []


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
    # Isolate the graphiti path: stub the prefixed-nomic index (which would otherwise
    # hit live FalkorDB) to empty so recall falls through to graphiti here.
    monkeypatch.setattr("friday.memory.recall_index.recall_facts", lambda q, k=5: [])
    facts = await store.recall("sarjan")
    assert facts == ["Sarjan works at Acme Corp", "Sutra is voice-native"]


class _ForgetGraphiti:
    """Fake graphiti: search returns two episodes; track what gets removed."""

    def __init__(self):
        self.removed: list[str] = []

    async def search(self, query, group_ids=None, num_results=10):
        class E:
            def __init__(self, fact, eps):
                self.fact, self.episodes = fact, eps
        return [E("fact one", ["ep1"]), E("fact two", ["ep2"])]

    async def remove_episode(self, uid):
        self.removed.append(uid)


def _ready_store(graphiti):
    s = MemoryStore()
    s._initialized = True
    s._available = True
    s._graphiti = graphiti
    return s


@pytest.mark.asyncio
async def test_forget_preview_deletes_nothing():
    """The hardening: apply=False must NEVER delete — it only previews."""
    g = _ForgetGraphiti()
    store = _ready_store(g)
    res = await store.forget("anything that matches", apply=False)
    assert res["removed"] == 0
    assert g.removed == []  # nothing deleted
    assert res["matched"] == ["fact one", "fact two"]
    assert set(res["episodes"]) == {"ep1", "ep2"}


@pytest.mark.asyncio
async def test_forget_apply_deletes_matched():
    g = _ForgetGraphiti()
    store = _ready_store(g)
    res = await store.forget("anything that matches", apply=True)
    assert res["removed"] == 2
    assert set(g.removed) == {"ep1", "ep2"}


@pytest.mark.asyncio
async def test_forget_skill_defaults_to_preview(monkeypatch):
    """The forget skill must preview (delete nothing) unless confirm=True."""
    from friday.skills import memory as memory_skill

    g = _ForgetGraphiti()
    monkeypatch.setattr(memory_skill, "get_store", lambda: _ready_store(g))
    out = await memory_skill.forget("some topic")  # confirm defaults to False
    assert g.removed == []
    assert out["count"] == 2
    assert "would_remove" in out


class _PersistStore:
    def __init__(self):
        self.saved: list[tuple[str, str]] = []

    async def remember(self, text, source=None):
        self.saved.append((text, source))
        return True


@pytest.mark.asyncio
async def test_persist_turn_async_saves_in_background(monkeypatch):
    """Voice turns persist fire-and-forget with source='voice', without blocking."""
    from friday.core import agent

    store = _PersistStore()
    monkeypatch.setattr(agent, "get_store", lambda: store)
    agent.persist_turn_async("what's my GPU?", "You have an RTX 3080 Ti.")
    assert store.saved == []  # not blocking — nothing saved synchronously
    await asyncio.sleep(0.02)  # let the background task run
    assert len(store.saved) == 1
    text, source = store.saved[0]
    assert source == "voice"
    assert "RTX 3080 Ti" in text


@pytest.mark.asyncio
async def test_persist_turn_async_skips_empty_reply(monkeypatch):
    from friday.core import agent

    store = _PersistStore()
    monkeypatch.setattr(agent, "get_store", lambda: store)
    agent.persist_turn_async("hi", "   ")
    await asyncio.sleep(0.02)
    assert store.saved == []
