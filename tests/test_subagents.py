"""Tests for the sub-agent delegation layer (isolated context, tool subsetting)."""

import json

from friday.core import subagents
from friday.core.subagents import SUBAGENTS, _tools_for, list_subagents, run_subagent
from friday.skills import registry


class _Fn:
    def __init__(self, name, arguments="{}"):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    """Minimal stand-in for an OpenAI tool_call (name / arguments / id)."""

    def __init__(self, name, arguments="{}", id="tc"):
        self.function = _Fn(name, arguments)
        self.id = id


# ── fakes for the router ─────────────────────────────────────────────────────
class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Resp:
    def __init__(self, msg):
        self.choices = [type("C", (), {"message": msg})()]


def _scripted_complete(*responses):
    """Return an async `complete` stand-in that yields the given responses in order."""
    queue = list(responses)

    async def _complete(messages, tier=None, tools=None, **kw):
        return queue.pop(0)

    return _complete


# ── structure ────────────────────────────────────────────────────────────────
def test_specialists_registered():
    assert set(SUBAGENTS) == {"architect", "researcher", "creative", "watchdog"}
    assert all(a.tools is None or isinstance(a.tools, frozenset) for a in SUBAGENTS.values())
    # No specialist can call `delegate`/`council` — prevents recursive delegation.
    assert all("delegate" not in (a.tools or set()) for a in SUBAGENTS.values())
    assert all("council" not in (a.tools or set()) for a in SUBAGENTS.values())


def test_list_subagents_pairs():
    pairs = dict(list_subagents())
    assert "researcher" in pairs and pairs["researcher"]


def test_tools_for_whitelist_matches_real_skills():
    registry.autodiscover()
    tools = _tools_for(SUBAGENTS["researcher"])
    names = {t["function"]["name"] for t in tools}
    assert names == {"web_search", "recall", "current_time", "weather"}
    # whitelisted names must be real registered skills, not typos
    assert names <= set(registry.list_skills())


# ── delegation flow ──────────────────────────────────────────────────────────
async def test_unknown_agent_returns_message():
    out = await run_subagent("nope", "do a thing")
    assert "Unknown sub-agent" in out and "architect" in out


async def test_direct_answer_no_tools(monkeypatch):
    monkeypatch.setattr(
        subagents, "complete", _scripted_complete(_Resp(_Msg(content="here is the plan")))
    )
    out = await run_subagent("architect", "design X")
    assert out == "here is the plan"


async def test_runs_a_tool_then_answers(monkeypatch):
    # First turn asks for a tool, second turn returns the final answer.
    monkeypatch.setattr(
        subagents,
        "complete",
        _scripted_complete(
            _Resp(_Msg(tool_calls=[object()])),
            _Resp(_Msg(content="42 hits found")),
        ),
    )

    async def _fake_run_tool_calls(tool_calls, confirm=None, allowed=None):
        return [{"role": "tool", "tool_call_id": "x", "name": "web_search", "content": "{}"}]

    monkeypatch.setattr("friday.core.agent._run_tool_calls", _fake_run_tool_calls)
    out = await run_subagent("researcher", "find X")
    assert out == "42 hits found"


def test_delegate_skill_registered():
    registry.autodiscover()
    assert "delegate" in registry.list_skills()
    assert registry.get("delegate").auto_confirm is True


# ── execution-time tool whitelist (closes GAP M1) ────────────────────────────
async def test_subagent_blocks_offlist_tool(monkeypatch):
    """A sub-agent that emits a call for a skill outside its whitelist has it
    blocked at execution — the skill is never actually invoked (defense-in-depth,
    not just schema filtering)."""
    ran: list[str] = []

    async def _fake_call(name, **kw):
        ran.append(name)
        return {"ok": True}

    monkeypatch.setattr(registry, "call", _fake_call)
    # researcher may call web_search/recall/current_time/weather — never run_command.
    monkeypatch.setattr(
        subagents,
        "complete",
        _scripted_complete(
            _Resp(_Msg(tool_calls=[_ToolCall("run_command", '{"cmd": "rm -rf /"}')])),
            _Resp(_Msg(content="done")),
        ),
    )
    out = await run_subagent("researcher", "do a thing")
    assert out == "done"
    assert "run_command" not in ran  # blocked before execution


async def test_run_tool_calls_allowed_none_runs_anything(monkeypatch):
    """allowed=None (the main-agent default) preserves old behaviour: any
    registered skill runs."""
    from friday.core.agent import _run_tool_calls

    ran: list[str] = []

    async def _fake_call(name, **kw):
        ran.append(name)
        return {"ok": True}

    class _Meta:
        auto_confirm = True
        timeout_s = 5

    monkeypatch.setattr(registry, "call", _fake_call)
    monkeypatch.setattr(registry, "get", lambda n: _Meta())

    res = await _run_tool_calls([_ToolCall("anything_at_all", "{}", "x")], allowed=None)
    assert ran == ["anything_at_all"]
    assert json.loads(res[0]["content"]) == {"ok": True}


async def test_run_tool_calls_blocks_outside_allowed(monkeypatch):
    """With an explicit allowed set, off-list calls are blocked, in-list ones run."""
    from friday.core.agent import _run_tool_calls

    ran: list[str] = []

    async def _fake_call(name, **kw):
        ran.append(name)
        return {"ok": True}

    class _Meta:
        auto_confirm = True
        timeout_s = 5

    monkeypatch.setattr(registry, "call", _fake_call)
    monkeypatch.setattr(registry, "get", lambda n: _Meta())

    res = await _run_tool_calls(
        [_ToolCall("current_time", "{}", "a"), _ToolCall("weather", "{}", "b")],
        allowed=frozenset({"current_time"}),
    )
    assert ran == ["current_time"]
    blocked = {r["name"]: json.loads(r["content"]).get("blocked") for r in res}
    assert blocked == {"current_time": None, "weather": True}


async def test_worker_runs_free_only_to_hold_zero_cost(monkeypatch):
    # A cheap worker must never escalate to a paid Claude tier — it requests free_only,
    # so a rate-limited fan-out can't quietly run up Claude charges.
    seen = {}

    async def _capture(messages, tier=None, tools=None, free_only=False, **kw):
        seen["free_only"] = free_only
        return _Resp(_Msg(content="done"))

    monkeypatch.setattr(subagents, "complete", _capture)
    out = await run_subagent("researcher", "find X")
    assert out == "done" and seen["free_only"] is True
