"""Tests for the council orchestrator (plan → parallel fan-out → critique → synthesize)."""

from friday.core import council as council_mod
from friday.core.council import CouncilResult, _coordinator_tier, _parse_delegations, convene
from friday.core.router import Tier
from friday.core.subagents import SUBAGENTS
from friday.skills import registry


class _Msg:
    def __init__(self, content):
        self.content = content


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": _Msg(content)})()]


def _seq_complete(*texts):
    """Async `complete` stand-in returning the given texts in order."""
    queue = list(texts)

    async def _complete(messages, tier=None, tools=None, **kw):
        return _Resp(queue.pop(0))

    return _complete


# ── roster + skill wiring ────────────────────────────────────────────────────
def test_creative_specialist_registered():
    assert "creative" in SUBAGENTS
    assert SUBAGENTS["creative"].tools == frozenset({"recall"})


def test_council_skill_registered_and_not_recursive():
    registry.autodiscover()
    assert "council" in registry.list_skills()
    assert registry.get("council").auto_confirm is True
    # No specialist may convene the council or delegate — no recursive fan-out.
    for sa in SUBAGENTS.values():
        assert "council" not in (sa.tools or set())
        assert "delegate" not in (sa.tools or set())


# ── delegation parsing (tolerant of weak local models) ───────────────────────
def test_parse_delegations_reads_json():
    plan = 'sure: [{"role":"researcher","task":"find X"},{"role":"creative","task":"draft Y"}]'
    assert _parse_delegations(plan, "goal") == [("researcher", "find X"), ("creative", "draft Y")]


def test_parse_delegations_falls_back_when_unparseable():
    assert _parse_delegations("let's just wing it", "the goal") == [
        ("researcher", "the goal"),
        ("creative", "the goal"),
    ]


def test_parse_delegations_drops_unknown_roles():
    plan = '[{"role":"ceo","task":"a"},{"role":"researcher","task":"b"}]'
    assert _parse_delegations(plan, "g") == [("researcher", "b")]


# ── full flow ────────────────────────────────────────────────────────────────
async def test_convene_full_flow(monkeypatch):
    seen_workers = []

    async def _fake_run_subagent(name, task, context=""):
        if name == "architect":
            return '[{"role":"researcher","task":"find facts"},{"role":"creative","task":"draft"}]'
        seen_workers.append(name)
        return f"{name} output"

    monkeypatch.setattr(council_mod, "run_subagent", _fake_run_subagent)
    # complete() is called for the critique first, then the synthesis.
    monkeypatch.setattr(council_mod, "complete", _seq_complete("looks solid", "final answer"))

    r = await convene("do a big multi-part thing")
    assert isinstance(r, CouncilResult)
    assert r.answer == "final answer"
    assert r.critique == "looks solid"
    assert set(r.contributions) == {"researcher", "creative"}
    assert set(seen_workers) == {"researcher", "creative"}


async def test_convene_parallel_fanout_runs_all_three(monkeypatch):
    seen = []

    async def _fake_run_subagent(name, task, context=""):
        if name == "architect":
            return ('[{"role":"researcher","task":"a"},'
                    '{"role":"creative","task":"b"},{"role":"watchdog","task":"c"}]')
        seen.append(name)
        return "ok"

    monkeypatch.setattr(council_mod, "run_subagent", _fake_run_subagent)
    monkeypatch.setattr(council_mod, "complete", _seq_complete("ok", "done"))

    r = await convene("x")
    assert set(seen) == {"researcher", "creative", "watchdog"}
    assert set(r.contributions) == {"researcher", "creative", "watchdog"}


async def test_convene_empty_goal_is_noop(monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("should not run for an empty goal")

    monkeypatch.setattr(council_mod, "run_subagent", _boom)
    r = await convene("   ")
    assert r.answer == "" and r.contributions == {}


# ── coordinator tier policy ($0 by default) ──────────────────────────────────
def test_coordinator_tier_free_by_default(monkeypatch):
    monkeypatch.setattr(council_mod, "allow_paid", lambda: False)
    assert _coordinator_tier() is Tier.MEDIUM


def test_coordinator_tier_paid_when_allowed_and_afforded(monkeypatch):
    monkeypatch.setattr(council_mod, "allow_paid", lambda: True)
    monkeypatch.setattr(
        council_mod, "get_budget", lambda: type("B", (), {"can_afford": lambda self, t: True})()
    )
    assert _coordinator_tier() is Tier.MAX
