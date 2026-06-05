"""Tests for conversation history — focuses on the in-memory fallback (Redis down).

Points at a dead Redis port so _connect fails and the store degrades to memory,
exercising the same path a real machine hits when the Redis container isn't up.
"""

from friday.core.history import History

_DEAD = "redis://localhost:59999"  # nothing listening → graceful fallback


def test_degrades_to_memory_when_redis_down():
    h = History(session="test", url=_DEAD)
    assert h.persistent is False


def test_append_and_load():
    h = History(session="test", url=_DEAD)
    h.append_turn("hi", "hello")
    assert h.load() == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_clear():
    h = History(session="test", url=_DEAD)
    h.append_turn("a", "b")
    h.clear()
    assert h.load() == []


def test_trims_to_max_turns():
    h = History(session="test", url=_DEAD, max_turns=4)
    for i in range(10):
        h.append("user", str(i))
    loaded = h.load()
    assert len(loaded) == 4
    assert loaded[-1]["content"] == "9"
