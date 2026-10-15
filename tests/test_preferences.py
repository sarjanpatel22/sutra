"""Tests for the preference skills (friday/skills/preferences.py).

Runs against a tmp_path SQLite store (the get_store() singleton is redirected),
so nothing touches the real ~/.local/share/friday/sutra.db.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from friday.skills.preferences import get_preference, set_preference
from friday.storage import sqlite as sqlite_mod
from friday.storage.sqlite import Store


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch) -> Iterator[Store]:
    store = Store(tmp_path / "prefs.db")
    monkeypatch.setattr(sqlite_mod, "_instance", store)
    yield store
    store.close()


async def test_set_then_get_roundtrip() -> None:
    res = await set_preference("units", "metric")
    assert res == {"status": "ok", "key": "units", "value": "metric"}
    got = await get_preference("units")
    assert got == {"key": "units", "value": "metric", "found": True}


async def test_get_missing_reports_not_found() -> None:
    got = await get_preference("nonexistent")
    assert got == {"key": "nonexistent", "value": None, "found": False}


async def test_get_all_returns_only_prefs_unprefixed() -> None:
    await set_preference("units", "metric")
    await set_preference("default_city", "Toronto")
    # a non-pref key in the same store must not leak into the listing
    sqlite_mod._instance.set("not_a_pref", "secret")  # type: ignore[union-attr]
    got = await get_preference()
    assert got == {"preferences": {"units": "metric", "default_city": "Toronto"}}


async def test_set_overwrites() -> None:
    await set_preference("voice", "am_adam")
    await set_preference("voice", "af_heart")
    got = await get_preference("voice")
    assert got["value"] == "af_heart"
