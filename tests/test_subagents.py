"""Tests for the sub-agent delegation layer (isolated context, tool subsetting)."""

from friday.core import subagents
from friday.core.subagents import SUBAGENTS, _tools_for, list_subagents, run_subagent
from friday.skills import registry


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
def test_three_specialists_registered():
    assert set(SUBAGENTS) == {"architect", "researcher", "watchdog"}
    assert all(a.tools is None or isinstance(a.tools, frozenset) for a in SUBAGENTS.values())
    # No specialist can call `delegate` — prevents recursive delegation.
    assert all("delegate" not in (a.tools or set()) for a in SUBAGENTS.values())


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

    async def _fake_run_tool_calls(tool_calls, confirm=None):
        return [{"role": "tool", "tool_call_id": "x", "name": "web_search", "content": "{}"}]

    monkeypatch.setattr("friday.core.agent._run_tool_calls", _fake_run_tool_calls)
    out = await run_subagent("researcher", "find X")
    assert out == "42 hits found"


def test_delegate_skill_registered():
    registry.autodiscover()
    assert "delegate" in registry.list_skills()
    assert registry.get("delegate").auto_confirm is True
