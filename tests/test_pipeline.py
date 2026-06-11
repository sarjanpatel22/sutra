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
    monkeypatch.setattr(agent, "persist_turn_async", lambda u, r: persisted.append((u, r)))
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
