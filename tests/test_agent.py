"""Tests for the agent tool-call loop — focused on the confirmation gate.

Before the fix, auto_confirm=False skills returned {"confirmation_required": True}
and could never actually run. Now they run iff a confirm callback approves them,
and are declined (never silently executed) otherwise.
"""

import json
import types

import pytest

from friday.core import agent
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
