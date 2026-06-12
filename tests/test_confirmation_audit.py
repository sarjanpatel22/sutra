"""The confirmation-gate invariant: no irreversible skill ships auto-confirmed, and
delegation can't bypass the gate.

Gate *enforcement* (declines without approval, runs only when approved) lives in
test_agent.py. This locks the *classification* surface (friday/skills/audit.py) so a
future state-mutating skill can't slip in auto-confirmed, and proves a sub-agent
declines an irreversible call end-to-end with the real `_run_tool_calls` — the one
bypass path test_subagents leaves mocked.
"""

import json
import types

from friday.core import subagents
from friday.core.subagents import run_subagent
from friday.skills import registry
from friday.skills.audit import (
    CANONICAL_GATED,
    all_problems,
    find_violations,
    mutation_hits,
    stale_exceptions,
)


def _metas():
    registry.autodiscover()
    return [registry.get(n) for n in registry.list_skills()]


# ── classification invariant ─────────────────────────────────────────────────
def test_no_auto_confirmed_skill_implies_mutation():
    # The false-negative tripwire: any auto-confirmed skill whose name implies a
    # mutating action (write/delete/run/…) must be a reviewed exception, else fail.
    assert find_violations(_metas()) == []


def test_canonical_irreversible_skills_exist_and_are_gated():
    metas = {m.name: m for m in _metas()}
    for name in CANONICAL_GATED:
        assert name in metas, f"canonical gated skill missing: {name}"
        assert metas[name].auto_confirm is False, f"{name} must require confirmation"


def test_reviewed_exceptions_are_not_stale():
    assert stale_exceptions(_metas()) == []


def test_live_registry_audit_is_clean():
    assert all_problems(_metas()) == []


def test_find_violations_catches_a_planted_false_negative():
    # Guard against a vacuous pass: an irreversible-looking skill shipped
    # auto-confirmed MUST be flagged by the tripwire.
    @registry.skill(name="_t_delete_everything", description="t", auto_confirm=True)
    async def _x() -> dict:
        return {}

    try:
        assert any("_t_delete_everything" in v for v in find_violations(_metas()))
    finally:
        registry._registry.pop("_t_delete_everything", None)


def test_mutation_hits_detects_verbs_in_names():
    assert mutation_hits("write_file") == {"write"}
    assert mutation_hits("run_command") == {"run"}
    assert mutation_hits("mcp__fs__delete_path") == {"delete"}
    assert mutation_hits("read_file") == set()
    assert mutation_hits("recall") == set()
    assert mutation_hits("current_time") == set()


# ── delegation can't bypass the gate (end-to-end, real _run_tool_calls) ───────
class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class _Resp:
    def __init__(self, msg):
        self.choices = [type("C", (), {"message": msg})()]


def _tc(name, args):
    fn = types.SimpleNamespace(name=name, arguments=json.dumps(args))
    return types.SimpleNamespace(id="1", function=fn)


def _scripted(*responses):
    queue = list(responses)

    async def _complete(messages, tier=None, tools=None, **kw):
        return queue.pop(0)

    return _complete


async def test_subagent_declines_irreversible_skill(monkeypatch):
    ran = {"v": False}

    @registry.skill(name="_t_sa_danger", description="t", auto_confirm=False)
    async def _danger(x: int = 0) -> dict:
        ran["v"] = True
        return {"ran": True}

    try:
        # The sub-agent's model tries to call the irreversible skill, then answers.
        monkeypatch.setattr(
            subagents,
            "complete",
            _scripted(
                _Resp(_Msg(tool_calls=[_tc("_t_sa_danger", {"x": 1})])),
                _Resp(_Msg(content="done without it")),
            ),
        )
        out = await run_subagent("researcher", "please write a file")
        assert ran["v"] is False  # declined inside the sub-agent — never executed
        assert out == "done without it"
    finally:
        registry._registry.pop("_t_sa_danger", None)
