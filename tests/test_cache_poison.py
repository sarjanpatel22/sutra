"""Tests for `friday.skills.cache_poison` — network fully mocked, no real requests."""

from __future__ import annotations

import pytest

from friday.skills import cache_poison as cp
from friday.skills.cache_poison import _bust, check_cache_poisoning


def test_bust_appends_unique_param() -> None:
    assert "sutracb=abc" in _bust("http://x.com/", "abc")
    assert _bust("http://x.com/?a=1", "abc").count("?") == 1  # uses & when query exists
    assert "&sutracb=abc" in _bust("http://x.com/?a=1", "abc")


async def test_confirmed_when_canary_persists_from_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # First (poison) request reflects the canary; the clean follow-up STILL returns it → cached.
    state = {"reflected_canary": None}

    def fake_req(url, extra):
        if extra:  # poison request carries the header → reflect its canary, "cache" it
            canary = next(iter(extra.values()))
            state["reflected_canary"] = canary
            return (f"<link href='//{canary}/x'>", {"x-cache": "miss"}, 200)
        # clean request: the cache now serves the previously-poisoned canary
        c = state["reflected_canary"]
        return (f"<link href='//{c}/x'>", {"x-cache": "hit", "age": "5"}, 200)

    monkeypatch.setattr(cp, "_req", fake_req)
    out = await check_cache_poisoning("http://x.com/")
    assert out["vulnerable"] is True and out["severity"] == "high"
    assert out["findings"][0]["id"].startswith("cache-poison-")
    assert out["cache_evidence"].get("x-cache") == "hit"


async def test_reflected_but_not_cached_is_not_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Poison reflects the canary, but the clean follow-up does NOT → a weak lead, not vulnerable.
    def fake_req(url, extra):
        if extra:
            canary = next(iter(extra.values()))
            return (f"host={canary}", {}, 200)
        return ("host=original.example", {}, 200)  # clean response is unpoisoned

    monkeypatch.setattr(cp, "_req", fake_req)
    out = await check_cache_poisoning("http://x.com/")
    assert out["vulnerable"] is False
    assert out["findings"] == []
    assert "REFLECTED but did not persist" in out["note"]


async def test_no_reflection_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cp, "_req", lambda url, extra: ("nothing reflected here", {}, 200))
    out = await check_cache_poisoning("x.com")  # also exercises scheme-prepend
    assert out["vulnerable"] is False
    assert "No unkeyed header reflected" in out["note"]


async def test_unreachable_yields_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cp, "_req", lambda url, extra: None)
    out = await check_cache_poisoning("http://x.com/")
    assert out["vulnerable"] is False


async def test_empty_url_errors() -> None:
    assert "error" in await check_cache_poisoning("")


def test_skill_is_confirmation_gated() -> None:
    from friday.skills import registry
    registry.autodiscover()
    assert registry.get("check_cache_poisoning").auto_confirm is False
