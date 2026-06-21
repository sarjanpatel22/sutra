"""process_text — the mic-free text-presence path (agent → TTS → orb → persist).

Mocks the heavy edges (TTS synth, audio playback, the agent stream) so the wiring
is testable with no GPU or speakers.
"""

from __future__ import annotations

import numpy as np
import pytest

from friday.voice import pipeline


class _Chunk:
    def __init__(self, audio):
        self.audio = audio


class _FakeTTS:
    sample_rate = 24000

    def stream(self, tokens):
        text = "".join(tokens)  # drain the token generator (runs until the None sentinel)
        yield _Chunk(np.zeros(max(1, len(text)) * 240, dtype=np.float32))


@pytest.fixture
def local_path(monkeypatch):
    """Wire process_text onto the local streaming path with fakes (no GPU/audio)."""
    from friday.core import agent

    played: list[int] = []
    persisted: list[tuple[str, str]] = []

    async def fake_voice_stream(text, history=None, tier=None):
        for tok in ("Hi ", "Sarjan."):
            yield tok

    monkeypatch.setattr(agent, "likely_needs_tool", lambda t: False)
    monkeypatch.setattr(agent, "cloud_tools_available", lambda: False)
    monkeypatch.setattr(agent, "voice_stream", fake_voice_stream)
    monkeypatch.setattr(agent, "persist_turn_async",
                        lambda u, r, **k: persisted.append((u, r)))
    monkeypatch.setattr(pipeline, "_get_tts", lambda: _FakeTTS())
    monkeypatch.setattr(pipeline, "_drive_orb_speaking", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "play_audio", lambda audio, **k: played.append(len(audio)))
    return played, persisted


@pytest.mark.asyncio
async def test_process_text_speaks_and_persists(local_path):
    played, persisted = local_path
    history: list[dict] = []
    reply = await pipeline.process_text("are you there?", history)
    assert reply == "Hi Sarjan."
    assert played  # TTS audio was played back
    assert history == [
        {"role": "user", "content": "are you there?"},
        {"role": "assistant", "content": "Hi Sarjan."},
    ]
    assert persisted == [("are you there?", "Hi Sarjan.")]  # typed turn persisted


@pytest.mark.asyncio
async def test_process_text_ignores_blank(local_path):
    history: list[dict] = []
    assert await pipeline.process_text("   ", history) == ""
    assert history == []  # nothing recorded for empty input


# --- speech-presence gate: background noise must never reach the LLM ---------------

class _FakeSTT:
    """A stand-in STT whose transcribe() returns a fixed result. Deliberately does NOT
    import friday.voice.stt (that pulls in faster_whisper, absent in CI) — process_audio
    only reads .text/.no_speech_prob/.avg_logprob, so a SimpleNamespace is enough."""

    def __init__(self, text, no_speech_prob):
        import types

        self._res = types.SimpleNamespace(
            text=text, no_speech_prob=no_speech_prob, avg_logprob=-0.3,
        )

    def transcribe(self, audio):
        return self._res


@pytest.fixture
def _spy_agent(monkeypatch):
    """Record whether the agent / command layer is reached during process_audio."""
    from friday.core import agent, commands

    reached: list[str] = []

    async def fake_try_command(text, confirm=None):
        reached.append("try_command")
        return None

    async def fake_voice_agent_run(*a, **k):
        reached.append("voice_agent_run")
        return "should not run"

    async def fake_voice_stream(*a, **k):
        reached.append("voice_stream")
        if False:  # make it an async generator
            yield ""

    monkeypatch.setattr(commands, "try_command", fake_try_command)
    monkeypatch.setattr(agent, "voice_agent_run", fake_voice_agent_run)
    monkeypatch.setattr(agent, "voice_stream", fake_voice_stream)
    monkeypatch.setattr(agent, "cloud_tools_available", lambda: False)
    monkeypatch.setattr(pipeline, "_get_tts", lambda: _FakeTTS())
    monkeypatch.setattr(pipeline, "_drive_orb_speaking", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "play_audio", lambda audio, **k: None)
    return reached


@pytest.mark.asyncio
async def test_process_audio_drops_background_noise(_spy_agent, monkeypatch):
    """High no_speech_prob (a TV in the background) is dropped BEFORE the command layer or
    the LLM — the root-cause fix for the rate-limit storm during the live test."""
    monkeypatch.setattr(pipeline, "_get_stt", lambda: _FakeSTT("crowd cheering noise", 0.96))
    history: list[dict] = []
    reply, hist, _lat, interrupted = await pipeline.process_audio(np.zeros(16000), history)
    assert reply == "" and interrupted is False
    assert hist == []                 # nothing recorded
    assert _spy_agent == []           # neither try_command nor the LLM was reached


@pytest.mark.asyncio
async def test_process_audio_processes_real_speech(_spy_agent, monkeypatch):
    """A confident transcript (low no_speech_prob) passes the gate and reaches the command
    layer — proving the gate isn't over-eager. A sentinel exception stops execution right
    at try_command, so the heavy TTS machinery never runs in the test."""
    monkeypatch.setattr(pipeline, "_get_stt", lambda: _FakeSTT("open firefox", 0.05))

    class _Reached(Exception):
        pass

    async def boom(text, confirm=None):
        raise _Reached  # "open firefox" → decide(act=True) → try_command reached past the gate

    from friday.core import commands
    monkeypatch.setattr(commands, "try_command", boom)

    with pytest.raises(_Reached):
        await pipeline.process_audio(np.zeros(16000), [])


# --- tutor mode: every turn coaches, never acts -------------------------------------

def test_asks_about_screen():
    assert pipeline._asks_about_screen("look at my screen")
    assert pipeline._asks_about_screen("what am I looking at")
    assert pipeline._asks_about_screen("can you read my screen")
    assert not pipeline._asks_about_screen("what's the next step")
    assert not pipeline._asks_about_screen("run nmap")


def test_set_tutor_mode_toggles():
    try:
        pipeline.set_tutor_mode(True)
        assert pipeline._tutor_mode_active() is True
        pipeline.set_tutor_mode(False)
        assert pipeline._tutor_mode_active() is False
    finally:
        pipeline.set_tutor_mode(False)


@pytest.mark.asyncio
async def test_process_text_tutor_mode_coaches_not_acts(local_path, monkeypatch):
    from friday.core import agent, commands

    _played, persisted = local_path
    seen: dict = {}

    async def fake_tutor(topic, *, history=None, screen=None, tier=None):
        seen["topic"] = topic
        return "First, what does an open port 80 usually mean?"

    async def boom_command(text, **k):
        seen["command_ran"] = True
        return {"spoken": "opened it", "skill": "open_app"}

    monkeypatch.setattr(agent, "tutor", fake_tutor)
    monkeypatch.setattr(commands, "try_command", boom_command)
    pipeline.set_tutor_mode(True)
    try:
        history: list[dict] = []
        reply = await pipeline.process_text("open firefox and tell me what to do next", history)
    finally:
        pipeline.set_tutor_mode(False)

    assert reply == "First, what does an open port 80 usually mean?"   # coached via agent.tutor
    assert seen.get("topic") == "open firefox and tell me what to do next"
    assert "command_ran" not in seen                                   # never acts in tutor mode
    assert persisted and persisted[-1] == (
        "open firefox and tell me what to do next", reply,
    )
