"""Tests for automatic tier selection — simple→MEDIUM (free), hard→HEAVY (Claude)."""

import pytest

import friday.core.autoroute as ar
from friday.core.autoroute import pick_tier, route
from friday.core.router import Tier


@pytest.mark.parametrize("text", [
    "what time is it?",
    "hi",
    "how much RAM do I have",
    "what's 17 times 23",
    "who won the game last night",
    "thanks!",
])
def test_simple_turns_stay_free(text):
    assert pick_tier(text) is Tier.MEDIUM


_FREE = {Tier.MEDIUM, Tier.CODE, Tier.LOCAL, Tier.REASON}


@pytest.mark.parametrize("text", [
    "go to tryhackme.com and start the first room",
    "open https://news.ycombinator.com and tell me the top story",
    "log in to my account and book the appointment",
    "analyze this error and figure out the fix",
    "compare these two approaches and recommend one",
    "research the best GPUs and summarize",
    "first open the site, and then fill out the form",
])
def test_hard_turns_stay_free_by_default(text, monkeypatch):
    monkeypatch.delenv("ROUTER_ALLOW_PAID", raising=False)
    assert pick_tier(text) in _FREE  # heavy classification, but never the paid tier


def test_coding_routes_to_free_code_tier(monkeypatch):
    monkeypatch.delenv("ROUTER_ALLOW_PAID", raising=False)
    assert pick_tier("refactor this function and write a unit test") is Tier.CODE


def test_long_request_stays_free(monkeypatch):
    monkeypatch.delenv("ROUTER_ALLOW_PAID", raising=False)
    assert pick_tier("please " + "detail " * 45) in _FREE


def test_paid_flag_enables_claude_for_heavy(monkeypatch):
    monkeypatch.setenv("ROUTER_ALLOW_PAID", "1")
    assert pick_tier("analyze https://example.com step by step") is Tier.HEAVY
    assert pick_tier("hello there") is Tier.MEDIUM  # simple still free even when paid allowed


def test_route_returns_reason(monkeypatch):
    monkeypatch.delenv("ROUTER_ALLOW_PAID", raising=False)
    tier, reason = route("open https://example.com")
    assert tier is Tier.MEDIUM and "free" in reason and "web" in reason
    tier, reason = route("hello there")
    assert tier is Tier.MEDIUM and reason


def test_word_boundary_avoids_false_heavy():
    # "plan" inside "explanation" / "planet" must NOT trip the heavy router
    assert pick_tier("give me a short explanation of planets") is Tier.MEDIUM


def test_paid_over_budget_falls_back_to_free(monkeypatch):
    monkeypatch.setenv("ROUTER_ALLOW_PAID", "1")

    class B:
        daily_pct = 100.0
        monthly_pct = 40.0
    monkeypatch.setattr(ar, "get_budget", lambda: B())
    # paid allowed, but the day's budget is spent → stay free
    assert pick_tier("analyze https://example.com step by step") in _FREE


def test_budget_error_does_not_break_routing(monkeypatch):
    monkeypatch.setenv("ROUTER_ALLOW_PAID", "1")

    def boom():
        raise RuntimeError("no budget file")
    monkeypatch.setattr(ar, "get_budget", boom)
    assert pick_tier("analyze this deeply") is Tier.HEAVY  # falls through cleanly
