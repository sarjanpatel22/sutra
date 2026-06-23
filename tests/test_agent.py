"""Tests for the agent tool-call loop — focused on the confirmation gate.

Before the fix, auto_confirm=False skills returned {"confirmation_required": True}
and could never actually run. Now they run iff a confirm callback approves them,
and are declined (never silently executed) otherwise.
"""

import json
import types

import pytest

from friday.core import agent
from friday.core.router import Tier
from friday.skills import registry


@pytest.fixture
def temp_skills():
    """Register a safe + an irreversible skill, then clean them out of the registry."""

    @registry.skill(name="_t_safe", description="t", auto_confirm=True)
    async def _safe(x: int = 1) -> dict:
        return {"ran": True, "x": x}

    @registry.skill(name="_t_danger", description="t", auto_confirm=False)
    async def _danger(x: int = 1) -> dict:
        return {"ran": True, "x": x}

    yield
    registry._registry.pop("_t_safe", None)
    registry._registry.pop("_t_danger", None)


def _tc(name: str, args: dict, call_id: str = "1"):
    """Build a minimal object shaped like a litellm tool_call."""
    fn = types.SimpleNamespace(name=name, arguments=json.dumps(args))
    return types.SimpleNamespace(id=call_id, function=fn)


def _content(result_msg: dict) -> dict:
    return json.loads(result_msg["content"])


async def test_safe_skill_runs_without_confirm(temp_skills):
    out = await agent._run_tool_calls([_tc("_t_safe", {"x": 5})])
    assert _content(out[0]) == {"ran": True, "x": 5}


async def test_irreversible_skill_declined_without_callback(temp_skills):
    # The whole point: no confirm hook → it does NOT run, and is not silently dropped.
    out = await agent._run_tool_calls([_tc("_t_danger", {"x": 5})])
    assert _content(out[0])["declined"] is True


async def test_irreversible_skill_runs_when_approved(temp_skills):
    async def approve(name: str, args: dict) -> bool:
        return True

    out = await agent._run_tool_calls([_tc("_t_danger", {"x": 9})], confirm=approve)
    assert _content(out[0]) == {"ran": True, "x": 9}


async def test_irreversible_skill_declined_when_rejected(temp_skills):
    async def reject(name: str, args: dict) -> bool:
        return False

    out = await agent._run_tool_calls([_tc("_t_danger", {"x": 9})], confirm=reject)
    assert _content(out[0])["declined"] is True


async def test_confirm_sees_skill_name_and_args(temp_skills):
    seen = {}

    async def spy(name: str, args: dict) -> bool:
        seen["name"], seen["args"] = name, args
        return True

    await agent._run_tool_calls([_tc("_t_danger", {"x": 42})], confirm=spy)
    assert seen == {"name": "_t_danger", "args": {"x": 42}}


async def test_unknown_skill_reports_error(temp_skills):
    out = await agent._run_tool_calls([_tc("_t_nonexistent", {})])
    assert "error" in _content(out[0])


# --- voice tool-routing heuristic --------------------------------------------

@pytest.mark.parametrize("query", [
    "what time is it",
    "what's the weather in Toronto",
    "how much RAM do I have",
    "calculate 5 times 12",
    "search for the latest news",
    "what's on my clipboard",
])
def test_likely_needs_tool_true(query):
    assert agent.likely_needs_tool(query) is True


@pytest.mark.parametrize("query", [
    "how are you",
    "tell me a joke",
    "what's your name",
    "good morning Sutra",
])
def test_likely_needs_tool_false(query):
    assert agent.likely_needs_tool(query) is False


def test_cloud_tools_available(monkeypatch):
    for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert agent.cloud_tools_available() is False
    monkeypatch.setenv("GROQ_API_KEY", "x")
    assert agent.cloud_tools_available() is True


async def test_voice_confirm_policy():
    """Voice runs web/browser tasks hands-free but declines destructive ops."""
    assert await agent._voice_confirm("web_task", {}) is True
    assert await agent._voice_confirm("fill_form", {}) is True
    for destructive in ("run_command", "run_python", "write_file", "edit_file",
                        "git_commit", "forget", "type_text"):
        assert await agent._voice_confirm(destructive, {}) is False


def test_spoken_strips_fake_tool_code():
    assert agent._spoken("```tool_code\nopen_website('x')\n```") == "Done."
    assert agent._spoken('{"action":"x"}') == "Done."
    assert agent._spoken("  There you go, love.  ") == "There you go, love."
    assert agent._spoken(None) == ""


# --- forced-tool failure honesty (rate-limit / local-fallback case) -----------

def _resp(content=None, tool_calls=None):
    """Build a minimal object shaped like a litellm completion response."""
    msg = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = types.SimpleNamespace(message=msg)
    return types.SimpleNamespace(choices=[choice])


@pytest.fixture
def _no_memory(monkeypatch):
    """Stub out memory recall + autodiscovery so voice_agent_run is offline/deterministic."""

    class _Store:
        async def recall(self, *a, **k):
            return []

    monkeypatch.setattr(agent, "get_store", lambda: _Store())
    monkeypatch.setattr(agent.registry, "autodiscover", lambda: None)
    monkeypatch.setattr(agent.registry, "all_tools", lambda: [])
    monkeypatch.setattr(agent.registry, "select_tools", lambda *a, **k: [])


async def test_forced_tool_no_call_returns_ratelimited(_no_memory, monkeypatch):
    # Router forced a tool, but the model (local fallback) only emitted text.
    async def fake_complete(messages, **kwargs):
        return _resp(content="Let me fire up Firefox... I couldn't get that to work")

    monkeypatch.setattr(agent, "complete", fake_complete)
    out = await agent.voice_agent_run("open my email", force_tool=True)
    assert out == agent._RATELIMITED_MSG


async def test_forced_tool_with_call_returns_real_reply(_no_memory, temp_skills, monkeypatch):
    # The model calls a tool on round 0, then gives a real answer → no rate-limit msg.
    calls = {"n": 0}

    async def fake_complete(messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp(tool_calls=[_tc("_t_safe", {"x": 7})])
        return _resp(content="Done, opened it.")

    monkeypatch.setattr(agent, "complete", fake_complete)
    out = await agent.voice_agent_run("open my email", force_tool=True)
    assert out == "Done, opened it."


async def test_unforced_text_reply_unchanged(_no_memory, monkeypatch):
    # Regression guard: force_tool=False text reply passes through _spoken unchanged.
    async def fake_complete(messages, **kwargs):
        return _resp(content="It's about three o'clock.")

    monkeypatch.setattr(agent, "complete", fake_complete)
    out = await agent.voice_agent_run("what's up", force_tool=False)
    assert out == "It's about three o'clock."


async def test_voice_prunes_tools_only_when_not_forced(_no_memory, monkeypatch):
    # Forced turn keeps the FULL toolset (recall-critical so tool_choice=required can't pick the
    # wrong tool); a conversational turn prunes to the turn-relevant subset.
    full = [{"function": {"name": f"t{i}"}} for i in range(40)]
    monkeypatch.setattr(agent.registry, "all_tools", lambda: full)
    monkeypatch.setattr(agent.registry, "select_tools", lambda *a, **k: full[:5])
    seen: dict = {}

    async def fake_complete(messages, **kwargs):
        seen["tools"] = kwargs.get("tools")
        return _resp(content="ok")

    monkeypatch.setattr(agent, "complete", fake_complete)

    await agent.voice_agent_run("open my email", force_tool=True)
    assert seen["tools"] == full              # forced → full set

    await agent.voice_agent_run("just chatting with you", force_tool=False)
    assert seen["tools"] == full[:5]          # not forced → pruned to relevant


# --- deterministic command short-circuit on the TEXT path (parity with voice) -----

@pytest.fixture
def _quiet(_no_memory, monkeypatch):
    """Silence persistence + turn logging so run() is deterministic and side-effect-free."""
    monkeypatch.setattr(agent, "persist_turn_async", lambda *a, **k: None)
    monkeypatch.setattr(agent, "log_text_turn", lambda *a, **k: None)
    monkeypatch.setattr(agent, "log_turn", lambda *a, **k: None)


async def test_run_open_command_acts_without_calling_the_llm(_quiet, monkeypatch):
    """'open notepad' on the text path must RUN the skill deterministically and never
    reach the LLM — the live bug was the model NARRATING 'opening notepad' while nothing
    happened, because the text path (unlike voice) didn't run the deterministic layer."""
    from friday.core import commands

    opened: list[str] = []

    async def fake_open_app(name):
        opened.append(name)
        return {"status": "launched", "app": name}

    monkeypatch.setattr(commands, "open_app", fake_open_app)

    llm_calls = {"n": 0}

    async def fake_complete(messages, **kwargs):
        llm_calls["n"] += 1
        return _resp(content="should never be reached")

    monkeypatch.setattr(agent, "complete", fake_complete)

    reply, history = await agent.run("open notepad")
    assert reply == "Opening notepad."
    assert opened == ["notepad"]            # the skill actually ran
    assert llm_calls["n"] == 0              # the LLM was never called — it ACTED, not narrated
    assert history[-1] == {"role": "assistant", "content": "Opening notepad."}


async def test_run_close_threads_confirm_hook(_quiet, monkeypatch):
    """A gated close on the text path honors the confirm hook, so `sutra chat` still asks
    before closing (the deterministic path must not bypass the confirmation gate)."""
    from friday.core import commands

    closed: list[str] = []

    async def fake_close(title):
        closed.append(title)
        return {"status": "closed"}

    monkeypatch.setattr(commands, "close_window", fake_close)

    async def fake_complete(messages, **kwargs):
        raise AssertionError("LLM must not be reached on a deterministic command")

    monkeypatch.setattr(agent, "complete", fake_complete)

    async def deny(name, args):
        return False

    reply, _ = await agent.run("close firefox", confirm=deny)
    assert closed == []                     # declined → window never closed
    assert "leave firefox open" in reply.lower()


async def test_run_non_command_still_falls_through_to_llm(_quiet, monkeypatch):
    """Regression guard: a conversational turn is NOT a command, so it still reaches the
    LLM exactly as before — the short-circuit must be conservative."""
    async def fake_complete(messages, **kwargs):
        return _resp(content="I'm doing well, thanks for asking!")

    monkeypatch.setattr(agent, "complete", fake_complete)
    monkeypatch.setattr(agent.registry, "all_tools", lambda: [])

    reply, _ = await agent.run("how are you doing today?")
    assert reply == "I'm doing well, thanks for asking!"


# --- auto-route by effort: big tasks escalate to the strong tier, cloud-first -------

async def test_run_auto_escalates_high_effort_tier(_quiet, monkeypatch):
    """A high-effort turn uses autoroute's stronger tier (Claude when paid) and goes
    cloud-first (local_tools_first=False), so big tasks don't degrade to the local 7B."""
    from friday.core import autoroute
    from friday.core.autoroute import Decision

    captured: dict = {}

    async def fake_complete(messages, tier=None, tools=None, local_tools_first=None, **kw):
        captured.update(tier=tier, lf=local_tools_first)
        return _resp(content="done")

    monkeypatch.setattr(agent, "complete", fake_complete)
    decision = Decision(True, Tier.HEAVY, "high", "big", tool_required=True)
    monkeypatch.setattr(autoroute, "decide", lambda *a, **k: decision)

    await agent.run("plan and build a whole thing for me", tier=Tier.MEDIUM)
    assert captured["tier"] == Tier.HEAVY   # escalated to the Claude tier automatically
    assert captured["lf"] is False          # cloud-first, not the local 7B


async def test_run_free_only_threads_to_complete(_quiet, monkeypatch):
    """free_only=True reaches the router, so the turn can never spend the paid budget — the
    contract the planner relies on for guaranteed-$0 step execution."""
    from friday.core import autoroute
    from friday.core.autoroute import Decision

    captured: dict = {}

    async def fake_complete(messages, tier=None, tools=None, free_only=False, **kw):
        captured["free_only"] = free_only
        return _resp(content="done")

    monkeypatch.setattr(agent, "complete", fake_complete)
    low = Decision(True, Tier.MEDIUM, "low", "x", tool_required=False)
    monkeypatch.setattr(autoroute, "decide", lambda *a, **k: low)
    monkeypatch.setattr(agent.registry, "all_tools", lambda: [])

    await agent.run("plan and build a whole thing for me", free_only=True)
    assert captured["free_only"] is True


async def test_run_does_not_downgrade_explicit_tier(_quiet, monkeypatch):
    """Escalate-only: a low-effort turn never downgrades an explicit higher --tier."""
    from friday.core import autoroute
    from friday.core.autoroute import Decision

    captured: dict = {}

    async def fake_complete(messages, tier=None, tools=None, local_tools_first=None, **kw):
        captured.update(tier=tier, lf=local_tools_first)
        return _resp(content="ok")

    monkeypatch.setattr(agent, "complete", fake_complete)
    decision = Decision(False, Tier.MEDIUM, "low", "chat")
    monkeypatch.setattr(autoroute, "decide", lambda *a, **k: decision)

    await agent.run("just chatting here", tier=Tier.MAX)
    assert captured["tier"] == Tier.MAX     # explicit MAX kept, not pulled down to MEDIUM
    assert captured["lf"] is None           # not high-effort → use the env default


# --- tool-result cap: don't re-send huge payloads through the model every round ----

def test_cap_tool_content_truncates_large(monkeypatch):
    monkeypatch.setenv("TOOL_RESULT_MAX_CHARS", "100")
    out = agent._cap_tool_content("x" * 500)
    assert out.startswith("x" * 100)
    assert "truncated 400 chars" in out
    assert len(out) < 500


def test_cap_tool_content_leaves_small_untouched(monkeypatch):
    monkeypatch.setenv("TOOL_RESULT_MAX_CHARS", "100")
    assert agent._cap_tool_content("small result") == "small result"


def test_cap_tool_content_zero_disables(monkeypatch):
    monkeypatch.setenv("TOOL_RESULT_MAX_CHARS", "0")
    big = "x" * 5000
    assert agent._cap_tool_content(big) == big  # 0 → no cap (full fidelity)


async def test_run_tool_calls_caps_large_result(monkeypatch, temp_skills):
    # A skill returning a huge payload is truncated in the tool message fed back to the model.
    monkeypatch.setenv("TOOL_RESULT_MAX_CHARS", "200")

    @registry.skill(name="_t_big", description="t", auto_confirm=True)
    async def _big() -> dict:
        return {"data": "y" * 4000}

    try:
        out = await agent._run_tool_calls([_tc("_t_big", {})])
        assert len(out[0]["content"]) < 4000
        assert "truncated" in out[0]["content"]
    finally:
        registry._registry.pop("_t_big", None)


def test_system_prompt_directs_code_for_hard_computation() -> None:
    # Measured lever (2026-06-14): raw 70B reasoning fails hard combinatorics
    # (coin-change), and self-consistency just amplifies the wrong answer — but the
    # SAME model is right when it WRITES AND RUNS code. The prompt must keep steering
    # quantitative turns to run_python rather than trusting mental math.
    p = agent.SYSTEM_PROMPT.lower()
    assert "run_python" in p
    assert "in your head" in p or "mental math" in p
