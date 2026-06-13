"""Stage-runner logic of scripts/voice_selftest.py — pure logic, no models/hardware.

Proves run_stage maps fn outcomes to PASS / FAIL / SKIPPED and that main()'s exit
code reflects whether any stage hard-failed (skips don't fail the run).
"""

from __future__ import annotations

import pytest

from scripts.voice_selftest import StageResult, StageSkip, main, run_stage


@pytest.mark.asyncio
async def test_run_stage_pass():
    async def fn() -> str:
        return "all good"

    r = await run_stage("X", fn)
    assert r == StageResult("X", "PASS", "all good")
    assert r.ok


@pytest.mark.asyncio
async def test_run_stage_fail_on_assertion():
    async def fn() -> str:
        raise AssertionError("nope")

    r = await run_stage("X", fn)
    assert r.status == "FAIL"
    assert "nope" in r.detail
    assert not r.ok


@pytest.mark.asyncio
async def test_run_stage_fail_on_unexpected_exception():
    async def fn() -> str:
        raise RuntimeError("boom")

    r = await run_stage("X", fn)
    assert r.status == "FAIL"
    assert "RuntimeError" in r.detail and "boom" in r.detail
    assert not r.ok


@pytest.mark.asyncio
async def test_run_stage_skip():
    async def fn() -> str:
        raise StageSkip("dependency down")

    r = await run_stage("X", fn)
    assert r.status == "SKIPPED"
    assert r.detail == "dependency down"
    assert r.ok  # a skip does not fail the run


@pytest.mark.asyncio
async def test_main_all_pass(monkeypatch):
    async def fake_tts(chain):
        import numpy as np

        chain.audio = np.ones(2400, dtype=np.float32)
        return "synth ok"

    async def fake_stt(chain):
        return "heard 'what time is it'"

    async def fake_agent():
        return "reply 'hello there friend'"

    import scripts.voice_selftest as vs

    monkeypatch.setattr(vs, "_tts_stage", fake_tts)
    monkeypatch.setattr(vs, "_stt_stage", fake_stt)
    monkeypatch.setattr(vs, "_agent_stage", fake_agent)
    assert await main() == 0


@pytest.mark.asyncio
async def test_main_returns_nonzero_when_a_stage_fails(monkeypatch):
    async def fake_tts(chain):
        raise AssertionError("TTS broke")

    async def fake_stt(chain):
        raise StageSkip("no audio from TTS stage")

    async def fake_agent():
        raise StageSkip("no LLM reachable")

    import scripts.voice_selftest as vs

    monkeypatch.setattr(vs, "_tts_stage", fake_tts)
    monkeypatch.setattr(vs, "_stt_stage", fake_stt)
    monkeypatch.setattr(vs, "_agent_stage", fake_agent)
    assert await main() == 1  # one FAIL -> nonzero, even though the rest skipped


@pytest.mark.asyncio
async def test_main_passes_when_agent_only_skips(monkeypatch):
    """STT/TTS pass + agent skipped (no LLM) is a healthy, $0/offline run."""

    async def fake_tts(chain):
        import numpy as np

        chain.audio = np.ones(2400, dtype=np.float32)
        return "synth ok"

    async def fake_stt(chain):
        return "heard 'what time is it'"

    async def fake_agent():
        raise StageSkip("no LLM reachable (offline)")

    import scripts.voice_selftest as vs

    monkeypatch.setattr(vs, "_tts_stage", fake_tts)
    monkeypatch.setattr(vs, "_stt_stage", fake_stt)
    monkeypatch.setattr(vs, "_agent_stage", fake_agent)
    assert await main() == 0
