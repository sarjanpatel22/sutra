"""Preference skills — let Sutra remember and recall your settings/preferences.

Backed by the SQLite structured store (not the graph): flat key/value settings
like preferred units, default city, voice choice.
"""

from __future__ import annotations

from typing import Any

from friday.skills.registry import skill
from friday.storage.sqlite import get_store

_PREFIX = "pref:"


@skill(
    name="set_preference",
    description="Remember a user preference or setting (e.g. units, default city, voice).",
    auto_confirm=True,
    timeout_s=5,
)
async def set_preference(key: str, value: str) -> dict[str, Any]:
    """Save a preference.

    key: short name, e.g. "units", "default_city", "voice".
    value: the value to store.
    """
    get_store().set(f"{_PREFIX}{key}", value)
    return {"status": "ok", "key": key, "value": value}


@skill(
    name="get_preference",
    description="Recall a saved user preference by name, or list all preferences.",
    auto_confirm=True,
    timeout_s=5,
)
async def get_preference(key: str = "") -> dict[str, Any]:
    """Get a preference, or all of them if key is blank.

    key: the preference name; blank returns every saved preference.
    """
    store = get_store()
    if not key:
        prefs = {k[len(_PREFIX):]: v for k, v in store.all().items() if k.startswith(_PREFIX)}
        return {"preferences": prefs}
    val = store.get(f"{_PREFIX}{key}")
    return {"key": key, "value": val, "found": val is not None}
