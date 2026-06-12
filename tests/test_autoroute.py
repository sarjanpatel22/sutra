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


@pytest.mark.parametrize("text", [
    "go to tryhackme.com and start the first room",
    "open https://news.ycombinator.com and tell me the top story",
    "log in to my account and book the appointment",
    "analyze this error and figure out the fix",
    "compare these two approaches and recommend one",
    "research the best GPUs and summarize",
    "refactor this function and write a unit test",
    "first open the site, and then fill out the form",
])
def test_hard_turns_go_heavy(text):
    assert pick_tier(text) is Tier.HEAVY


def test_long_request_goes_heavy():
    assert pick_tier("please " + "detail " * 45) is Tier.HEAVY


def test_route_returns_reason():
    tier, reason = route("open https://example.com")
    assert tier is Tier.HEAVY and "URL" in reason
    tier, reason = route("hello there")
    assert tier is Tier.MEDIUM and reason


def test_word_boundary_avoids_false_heavy():
    # "plan" inside "explanation" / "planet" must NOT trip the heavy router
    assert pick_tier("give me a short explanation of planets") is Tier.MEDIUM


def test_budget_exhausted_forces_free(monkeypatch):
    class B:
        daily_pct = 100.0
        monthly_pct = 40.0
    monkeypatch.setattr(ar, "get_budget", lambda: B())
    # even a clear heavy task stays free when the day's budget is spent
    tier, reason = route("analyze https://example.com step by step")
    assert tier is Tier.MEDIUM and "budget" in reason


def test_budget_error_does_not_break_routing(monkeypatch):
    def boom():
        raise RuntimeError("no budget file")
    monkeypatch.setattr(ar, "get_budget", boom)
    assert pick_tier("analyze this deeply") is Tier.HEAVY  # falls through cleanly
