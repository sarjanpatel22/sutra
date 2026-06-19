"""Tests for the SQLite KV store (friday/storage/sqlite.py) — the structured personal
store behind preferences/settings. Everything runs against a tmp_path DB; nothing
touches the real ~/.local/share/friday/sutra.db."""

from __future__ import annotations

import threading

import pytest

from friday.storage import sqlite as sqlite_mod
from friday.storage.sqlite import Store


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "test.db")


# --- set/get roundtrip -----------------------------------------------------

@pytest.mark.parametrize("value", [
    "a string",
    42,
    3.14,
    True,
    False,
    None,
    ["a", "list", 1, 2],
    {"nested": {"dict": [1, 2, 3]}, "ok": True},
    [],
    {},
])
def test_set_get_roundtrip_preserves_json_types(store: Store, value) -> None:
    store.set("k", value)
    assert store.get("k") == value


def test_get_missing_returns_none_by_default(store: Store) -> None:
    assert store.get("nope") is None


def test_get_missing_returns_custom_default(store: Store) -> None:
    assert store.get("nope", default="fallback") == "fallback"


def test_stored_none_is_distinguished_from_missing(store: Store) -> None:
    # A key explicitly set to None must read back as None — NOT the default — because
    # the row exists. This is the subtle contract that makes None a storable value.
    store.set("present_none", None)
    assert store.get("present_none", default="DEFAULT") is None
    assert store.get("absent", default="DEFAULT") == "DEFAULT"


# --- upsert ---------------------------------------------------------------

def test_set_overwrites_existing_key(store: Store) -> None:
    store.set("k", "first")
    store.set("k", "second")
    assert store.get("k") == "second"
    assert len(store.keys()) == 1  # upsert, not a second row


# --- delete ---------------------------------------------------------------

def test_delete_existing_returns_true_and_removes(store: Store) -> None:
    store.set("k", 1)
    assert store.delete("k") is True
    assert store.get("k") is None


def test_delete_missing_returns_false(store: Store) -> None:
    assert store.delete("never") is False


# --- keys -----------------------------------------------------------------

def test_keys_returns_all_sorted(store: Store) -> None:
    store.set("c", 1)
    store.set("a", 2)
    store.set("b", 3)
    assert store.keys() == ["a", "b", "c"]


def test_keys_filters_by_prefix(store: Store) -> None:
    store.set("app.theme", 1)
    store.set("app.lang", 2)
    store.set("user.name", 3)
    assert store.keys("app.") == ["app.lang", "app.theme"]


def test_keys_prefix_with_underscore_is_literal_not_wildcard(store: Store) -> None:
    # Regression: SQLite LIKE treats _ as a single-char wildcard. A prefix of "user_"
    # must match ONLY keys literally starting with "user_", not "users"/"userid".
    store.set("users", 1)
    store.set("user_pref", 2)
    store.set("userid", 3)
    store.set("user_theme", 4)
    assert store.keys("user_") == ["user_pref", "user_theme"]


def test_keys_prefix_with_percent_is_literal(store: Store) -> None:
    store.set("100%done", 1)
    store.set("100xdone", 2)
    assert store.keys("100%") == ["100%done"]


def test_keys_empty_prefix_matches_all(store: Store) -> None:
    store.set("x", 1)
    store.set("y", 2)
    assert store.keys("") == ["x", "y"]


# --- all ------------------------------------------------------------------

def test_all_returns_decoded_mapping(store: Store) -> None:
    store.set("a", {"v": 1})
    store.set("b", [1, 2])
    assert store.all() == {"a": {"v": 1}, "b": [1, 2]}


def test_all_empty_store_is_empty_dict(store: Store) -> None:
    assert store.all() == {}


# --- persistence ----------------------------------------------------------

def test_data_persists_across_instances(tmp_path) -> None:
    path = tmp_path / "persist.db"
    s1 = Store(path)
    s1.set("kept", {"across": "restarts"})
    s1.close()
    # A fresh Store on the same file must see the committed data.
    s2 = Store(path)
    assert s2.get("kept") == {"across": "restarts"}


def test_creates_parent_directory(tmp_path) -> None:
    nested = tmp_path / "deep" / "nested" / "dir" / "store.db"
    assert not nested.parent.exists()
    s = Store(nested)
    s.set("k", 1)
    assert nested.exists()
    assert s.get("k") == 1


# --- singleton ------------------------------------------------------------

def test_get_store_returns_singleton(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sqlite_mod, "_DB_PATH", tmp_path / "singleton.db")
    monkeypatch.setattr(sqlite_mod, "_instance", None)
    a = sqlite_mod.get_store()
    b = sqlite_mod.get_store()
    assert a is b


# --- concurrency ----------------------------------------------------------

def test_concurrent_writes_all_land(store: Store) -> None:
    # The store is shared across async handlers + TTS/persist threads, guarded by an
    # internal lock (check_same_thread=False). Hammer it from many threads and assert
    # every write committed intact — no lost updates, no corruption.
    n = 50

    def writer(i: int) -> None:
        store.set(f"key{i:03d}", {"i": i})

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.keys()) == n
    for i in range(n):
        assert store.get(f"key{i:03d}") == {"i": i}
