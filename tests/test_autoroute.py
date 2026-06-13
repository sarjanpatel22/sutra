"""Tests for the auto router — one brain decides act-vs-chat + effort, free by default."""

import pytest

import friday.core.autoroute as ar
from friday.core.autoroute import decide, pick_tier, route
from friday.core.router import Tier

_FREE = {Tier.MEDIUM, Tier.CODE, Tier.LOCAL, Tier.REASON}


@pytest.mark.parametrize("text", [
    "hi", "thanks!", "what do you think of that", "tell me a joke", "good morning",
])
def test_chitchat_does_not_act_and_stays_free(text):
    d = decide(text)
    assert d.act is False and d.effort == "low" and d.tier in _FREE


@pytest.mark.parametrize("text", [
    "what time is it?",
    "how much RAM do I have",
    "search for the latest news",
    "what's the weather",
    "do you remember my sister's name",
])
def test_light_tool_turns_act_at_medium(text):
    d = decide(text)
    assert d.act is True and d.effort == "medium" and d.tier is Tier.MEDIUM


@pytest.mark.parametrize("text", [
    "go to tryhackme.com and start the first room",
    "open https://news.ycombinator.com and tell me the top story",
    "log in to my account and book the appointment",
    "analyze this error and figure out the fix",
    "compare these two approaches and recommend one",
    "first open the site, and then fill out the form",
])
def test_high_turns_act_and_stay_free_by_default(text, monkeypatch):
    monkeypatch.delenv("ROUTER_ALLOW_PAID", raising=False)
    d = decide(text)
    assert d.act is True and d.effort == "high" and d.tier in _FREE


def test_tryhackme_phrase_now_goes_agentic():
    # The exact phrase that silently fell to local chat before — must act now.
    d = decide("go to tryhackme.com and start a room")
    assert d.act is True and d.effort == "high"


def test_coding_routes_to_free_code_tier(monkeypatch):
    monkeypatch.delenv("ROUTER_ALLOW_PAID", raising=False)
    d = decide("refactor this function and write a unit test")
    assert d.act is True and d.tier is Tier.CODE and d.effort == "high"


def test_long_request_is_high_and_free(monkeypatch):
    monkeypatch.delenv("ROUTER_ALLOW_PAID", raising=False)
    d = decide("please " + "detail " * 45)
    assert d.act is True and d.effort == "high" and d.tier in _FREE


def test_paid_flag_enables_claude_for_high(monkeypatch):
    monkeypatch.setenv("ROUTER_ALLOW_PAID", "1")
    assert pick_tier("analyze https://example.com step by step") is Tier.HEAVY
    assert pick_tier("hello there") is Tier.MEDIUM  # chit-chat free even when paid allowed


def test_route_returns_reason(monkeypatch):
    monkeypatch.delenv("ROUTER_ALLOW_PAID", raising=False)
    tier, reason = route("open https://example.com")
    assert tier is Tier.MEDIUM and "free" in reason and "web" in reason
    tier, reason = route("hello there")
    assert tier is Tier.MEDIUM and reason


def test_word_boundary_avoids_false_high():
    # "plan" inside "explanation" / "planet" must NOT trip the high router.
    d = decide("give me a short explanation of planets")
    assert d.effort != "high"


def test_paid_over_budget_falls_back_to_free(monkeypatch):
    monkeypatch.setenv("ROUTER_ALLOW_PAID", "1")

    class B:
        daily_pct = 100.0
        monthly_pct = 40.0
    monkeypatch.setattr(ar, "get_budget", lambda: B())
    assert pick_tier("analyze https://example.com step by step") in _FREE


def test_budget_error_does_not_break_routing(monkeypatch):
    monkeypatch.setenv("ROUTER_ALLOW_PAID", "1")

    def boom():
        raise RuntimeError("no budget file")
    monkeypatch.setattr(ar, "get_budget", boom)
    assert pick_tier("analyze this deeply") is Tier.HEAVY  # falls through cleanly
