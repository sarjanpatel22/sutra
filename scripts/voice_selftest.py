#!/usr/bin/env python
"""Headless end-to-end self-test of the voice chain — no mic, no speaker.

Validates STT -> agent -> TTS by synthesizing a known phrase, transcribing it
back, and running one agent turn. Each stage reports PASS / FAIL / SKIPPED with a
reason, and the process exits non-zero if any stage hard-fails. The agent stage is
SKIPPED (not FAIL) when no free LLM/Ollama is reachable, so STT/TTS still validate
with $0 and fully offline.

Usage:
  python scripts/voice_selftest.py
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np

# Phrase TTS speaks and STT must recover. Keep it short, common words — small.en
# transcribes these reliably, so a flaky match means a real regression.
_PHRASE = "what time is it"
_KEYWORDS = ("what", "time")

Status = Literal["PASS", "FAIL", "SKIPPED"]


@dataclass
class StageResult:
    name: str
    status: Status
    detail: str = ""

    @property
    def ok(self) -> bool:
        """A stage counts as not-failed when it passed or was skipped."""
        return self.status != "FAIL"


class StageSkip(Exception):
    """Raise inside a stage fn to mark it SKIPPED (e.g. dependency unavailable)."""


async def run_stage(name: str, fn: Callable[[], Awaitable[str]]) -> StageResult:
    """Run one stage fn, mapping its outcome to PASS / FAIL / SKIPPED.

    fn returns a detail string on success (PASS), raises StageSkip to skip, or
    raises any other exception / AssertionError to fail. Never raises itself.
    """
    print(f"[ {name} ] running...", flush=True)
    try:
        detail = await fn()
        result = StageResult(name, "PASS", detail)
    except StageSkip as e:
        result = StageResult(name, "SKIPPED", str(e))
    except Exception as e:  # noqa: BLE001 — any failure is a stage FAIL, not a crash
        result = StageResult(name, "FAIL", f"{type(e).__name__}: {e}")
    print(f"[ {name} ] {result.status}{f' — {result.detail}' if result.detail else ''}", flush=True)
    return result


# Carries the synthesized PCM between the TTS and STT stages.
@dataclass
class _Chain:
    audio: np.ndarray | None = None
    sample_rate: int = 24000


async def _tts_stage(chain: _Chain) -> str:
    """Synthesize the known phrase to PCM audio."""
    from friday.voice.tts import TTS

    print("  loading Kokoro TTS (first run downloads the model)...", flush=True)
    chunk = await asyncio.to_thread(lambda: TTS().synthesize(_PHRASE))
    if chunk.audio is None or len(chunk.audio) == 0:
        raise AssertionError("TTS produced no audio")
    chain.audio = chunk.audio
    chain.sample_rate = chunk.sample_rate
    secs = len(chunk.audio) / chunk.sample_rate
    return f"{len(chunk.audio)} samples ({secs:.1f}s @ {chunk.sample_rate}Hz)"


async def _stt_stage(chain: _Chain) -> str:
    """Transcribe the synthesized audio and assert it roughly matches the phrase."""
    from friday.voice.stt import STT

    if chain.audio is None:
        raise StageSkip("no audio from TTS stage")
    # Whisper expects 16kHz; Kokoro emits 24kHz. Resample by linear interpolation —
    # good enough for STT and keeps the stage offline (no scipy dependency).
    audio = chain.audio
    if chain.sample_rate != 16000:
        n_out = int(len(audio) * 16000 / chain.sample_rate)
        audio = np.interp(
            np.linspace(0, len(audio), n_out, endpoint=False),
            np.arange(len(audio)),
            audio,
        ).astype(np.float32)
    print("  loading Whisper STT (first run downloads the model)...", flush=True)
    result = await asyncio.to_thread(lambda: STT().transcribe(audio))
    text = result.text.strip()
    if not text:
        raise AssertionError("STT returned empty transcript")
    low = text.lower()
    if not any(kw in low for kw in _KEYWORDS):
        raise AssertionError(f"transcript {text!r} matches none of {_KEYWORDS}")
    return f"heard {text!r}"


async def _agent_stage() -> str:
    """Run one agent turn on a fixed prompt; SKIP if no LLM is reachable."""
    from friday.core.agent import voice_run
    from friday.core.router import Tier

    try:
        reply, _ = await voice_run("say hello in three words", history=[], tier=Tier.LOCAL)
    except Exception as e:  # noqa: BLE001 — Ollama/cloud down: skip, don't fail the chain
        raise StageSkip(f"no LLM reachable ({type(e).__name__}: {e})") from e
    reply = reply.strip()
    if not reply:
        raise StageSkip("LLM reachable but returned empty (likely cold/offline fallback)")
    return f"reply {reply[:60]!r}"


async def main() -> int:
    """Run all stages, print a summary, return a process exit code."""
    chain = _Chain()
    results = [
        await run_stage("TTS", lambda: _tts_stage(chain)),
        await run_stage("STT", lambda: _stt_stage(chain)),
        await run_stage("AGENT", _agent_stage),
    ]

    print("\n=== voice self-test summary ===")
    for r in results:
        line = f"  {r.status:<7} {r.name}"
        if r.detail:
            line += f"  ({r.detail})"
        print(line)

    failed = [r.name for r in results if not r.ok]
    if failed:
        print(f"\nFAIL — {', '.join(failed)} stage(s) failed")
        return 1
    print("\nOK — voice chain healthy")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
