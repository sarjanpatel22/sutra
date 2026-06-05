"""Tests for the SQLite structured store + preference skills."""

import friday.storage.sqlite as sqlite_mod
from friday.storage.sqlite import Store


def test_set_get_default(tmp_path):
    s = Store(tmp_path / "t.db")
    s.set("a", {"x": 1})
    assert s.get("a") == {"x": 1}
    assert s.get("missing", "fallback") == "fallback"


def test_overwrite(tmp_path):
    s = Store(tmp_path / "t.db")
    s.set("k", "v1")
    s.set("k", "v2")
    assert s.get("k") == "v2"


def test_delete(tmp_path):
    s = Store(tmp_path / "t.db")
    s.set("k", "v")
    assert s.delete("k") is True
    assert s.delete("k") is False
    assert s.get("k") is None


def test_keys_prefix_and_all(tmp_path):
    s = Store(tmp_path / "t.db")
    s.set("pref:a", 1)
    s.set("pref:b", 2)
    s.set("other", 3)
    assert s.keys("pref:") == ["pref:a", "pref:b"]
    assert s.all() == {"pref:a": 1, "pref:b": 2, "other": 3}


def test_persists_across_instances(tmp_path):
    path = tmp_path / "t.db"
    Store(path).set("k", "v")
    assert Store(path).get("k") == "v"


async def test_preference_skills(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_mod, "_instance", None)
    monkeypatch.setattr(sqlite_mod, "_DB_PATH", tmp_path / "prefs.db")
    from friday.skills.preferences import get_preference, set_preference

    await set_preference("units", "metric")
    got = await get_preference("units")
    assert got["value"] == "metric" and got["found"] is True
    assert (await get_preference(""))["preferences"]["units"] == "metric"
    assert (await get_preference("nope"))["found"] is False
