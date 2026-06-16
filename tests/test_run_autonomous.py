"""Tests for the hands-free autonomous run loop (`sutra auto` / `pwn`)."""

import json
import types

from friday.core import agent


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


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


async def test_refuses_without_autonomous(monkeypatch):
    monkeypatch.delenv("SUTRA_AUTONOMOUS", raising=False)
    report, msgs = await agent.run_autonomous("scan the box")
    assert "SUTRA_AUTONOMOUS" in report and msgs == []


async def test_runs_rounds_auto_approves_and_reports(monkeypatch, tmp_path):
    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)
    monkeypatch.setattr(agent, "complete", _scripted(
        _Resp(_Msg(tool_calls=[_tc("run_command", {"command": "nmap 10.10.10.10"})])),
        _Resp(_Msg(content="still enumerating")),               # tool-less → nudge
        _Resp(_Msg(content="Found SSH on 22. Flag THM-x")),     # post-nudge final
    ))
    approved = {}

    async def _fake_run_tool_calls(tool_calls, confirm=None, observe=None, allowed=None):
        approved["ok"] = await confirm("run_command", {"command": "nmap 10.10.10.10"})
        return [{"role": "tool", "tool_call_id": "x", "name": "run_command", "content": "{}"}]

    monkeypatch.setattr(agent, "_run_tool_calls", _fake_run_tool_calls)

    report, msgs = await agent.run_autonomous("scan it", max_rounds=5)
    assert report == "Found SSH on 22. Flag THM-x"
    assert approved["ok"] is True  # gated run_command auto-approved under SUTRA_AUTONOMOUS
    # the early-stopping nudge fired
    assert any("operating autonomously" in (m.get("content") or "") for m in msgs)
    # engagement file persisted
    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1 and "## Report" in files[0].read_text()


async def test_round_cap_forces_final_report(monkeypatch, tmp_path):
    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)
    calls = {"n": 0}

    async def _complete(messages, tier=None, tools=None, **kw):
        calls["n"] += 1
        if tools is None:  # the post-cap final-report request
            return _Resp(_Msg(content="out of rounds; partial findings"))
        return _Resp(_Msg(tool_calls=[_tc("run_command", {"command": "nmap"})]))

    async def _fake_run_tool_calls(tool_calls, confirm=None, observe=None, allowed=None):
        return [{"role": "tool", "tool_call_id": "x", "name": "run_command", "content": "{}"}]

    monkeypatch.setattr(agent, "complete", _complete)
    monkeypatch.setattr(agent, "_run_tool_calls", _fake_run_tool_calls)

    report, _ = await agent.run_autonomous("scan", max_rounds=3)
    assert "out of rounds" in report
    assert calls["n"] == 4  # 3 tool rounds + 1 final wrap-up


async def test_voice_confirm_alias_preserved(monkeypatch):
    # The rename kept the old name as an alias (voice path + test_autonomous import it).
    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    assert agent._voice_confirm is agent._autonomous_confirm
    assert await agent._voice_confirm("run_command", {"command": "ls"}) is True
