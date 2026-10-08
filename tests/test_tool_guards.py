"""Trust guards in the agent tool-call loop.

Deterministic, $0 defenses against the two most common agentic hallucinations —
a model inventing a tool name, and a model omitting/inventing arguments — caught
BEFORE the skill runs so a bad call can't cause a wrong action or a phantom success.
"""
import json
from types import SimpleNamespace

from friday.core import agent
from friday.skills import registry


def _tc(name, args, tid="t1"):
    """A minimal stand-in for an OpenAI tool_call object."""
    return SimpleNamespace(id=tid, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _payload(messages):
    return json.loads(messages[0]["content"])


def _register_echo() -> str:
    @registry.skill(name="_guard_echo", description="Echo a message back.")
    async def _echo(message: str, times: int = 1) -> str:  # message required, times optional
        return message * times

    return "_guard_echo"


async def test_unknown_tool_suggests_closest():
    name = _register_echo()
    try:
        r = _payload(await agent._run_tool_calls([_tc("_guard_ekho", {"message": "hi"})]))
        assert r["error"].startswith("Unknown skill")
        assert name in r["did_you_mean"]  # self-correction hint, not a dead end
    finally:
        registry.unregister(name)


async def test_missing_required_arg_blocked_before_run():
    name = _register_echo()
    try:
        r = _payload(await agent._run_tool_calls([_tc(name, {"times": 2})]))  # omits 'message'
        assert r["error"] == "missing required arguments"
        assert "message" in r["missing_required"]
        assert "message" in r["expected_params"]
    finally:
        registry.unregister(name)


async def test_valid_call_still_runs():
    name = _register_echo()
    try:
        r = await agent._run_tool_calls([_tc(name, {"message": "ab", "times": 3})])
        assert json.loads(r[0]["content"]) == "ababab"  # the skill actually executed
    finally:
        registry.unregister(name)


async def test_invented_param_error_returns_expected_params():
    name = _register_echo()
    try:
        r = _payload(await agent._run_tool_calls([_tc(name, {"message": "x", "nope": 1})]))
        assert "error" in r  # TypeError from the invented kwarg
        assert "message" in r.get("expected_params", [])  # model is handed the valid params
    finally:
        registry.unregister(name)


def test_guard_helpers_direct():
    name = _register_echo()
    try:
        assert agent._tool_param_names(name) == ["message", "times"]
        assert agent._missing_required_args(registry.get(name), {"times": 1}) == ["message"]
        assert agent._missing_required_args(registry.get(name), {"message": "x"}) == []
        assert agent._tool_param_names("totally_not_a_skill") == []
        assert "did_you_mean" in agent._unknown_skill_result("totally_not_a_skill")
    finally:
        registry.unregister(name)
