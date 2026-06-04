"""Voice pipeline: mic → VAD → Whisper STT → agent → Kokoro TTS → speaker.

Two modes:
  push_to_talk()  — hold spacebar to record, release to process
  vad_listen()    — continuous VAD, auto-detects speech start/end

Latency is measured and logged at every stage.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue as q_mod
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from friday.voice.audio import play_audio, record_with_vad, record_while_held
from friday.voice.vad import VAD

_log = logging.getLogger("sutra.voice")
_LATENCY_LOG = Path.home() / ".local" / "share" / "friday" / "voice_latency.jsonl"

_stt_instance = None
_tts_instance = None


def _get_stt():
    global _stt_instance
    if _stt_instance is None:
        from friday.voice.stt import STT
        _stt_instance = STT()
    return _stt_instance


def _get_tts():
    global _tts_instance
    if _tts_instance is None:
        from friday.voice.tts import TTS
        _tts_instance = TTS()
    return _tts_instance


def prewarm() -> None:
    """Pre-load STT, TTS, and the local LLM so the first voice turn isn't cold.

    Without the LLM warm-up the first turn pays a ~3s Ollama model-load on top of
    inference — pre-flight measured cold TTFT 3016ms vs warm ~450ms.
    """
    import numpy as np
    _log.info("pre-warming STT...")
    stt = _get_stt()
    stt.transcribe(np.zeros(16000, dtype=np.int16))
    _log.info("pre-warming TTS...")
    tts = _get_tts()
    tts.synthesize("warm")
    _log.info("pre-warming local LLM...")
    try:
        import asyncio

        from friday.core.agent import voice_stream
        from friday.core.router import Tier

        async def _warm() -> None:
            # Also warms the memory embedder (recall now runs each voice turn).
            from friday.memory.store import get_store
            await get_store().recall("warmup", limit=1)
            async for _ in voice_stream("hi", history=[], tier=Tier.LOCAL):
                break  # first token means the model is resident in VRAM

        asyncio.run(_warm())
    except Exception as e:  # noqa: BLE001 — Ollama down: voice still works, just cold
        _log.warning("LLM pre-warm skipped (%s)", e)
    _log.info("voice models ready")


@dataclass
class TurnLatency:
    """Per-stage latency for one voice turn."""
    stt_ms: float = 0.0
    agent_ttft_ms: float = 0.0
    tts_first_ms: float = 0.0
    total_ms: float = 0.0


@dataclass
class VoiceTurn:
    transcript: str
    reply_text: str
    latency: TurnLatency
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))


def _log_latency(turn: VoiceTurn) -> None:
    import json
    _LATENCY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(_LATENCY_LOG, "a") as f:
        f.write(json.dumps({
            "ts": turn.timestamp,
            "transcript": turn.transcript[:80],
            "stt_ms": round(turn.latency.stt_ms),
            "agent_ttft_ms": round(turn.latency.agent_ttft_ms),
            "tts_first_ms": round(turn.latency.tts_first_ms),
            "total_ms": round(turn.latency.total_ms),
        }) + "\n")


async def process_audio(
    audio: np.ndarray,
    history: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], TurnLatency]:
    """STT → streaming agent → streaming TTS → play.

    Returns (reply_text, updated_history, latency).
    history is extended in-place and also returned.
    """
    from friday.core.agent import voice_stream
    from friday.core.router import Tier

    lat = TurnLatency()
    t_total = time.monotonic()

    # STT
    stt = _get_stt()
    t0 = time.monotonic()
    result = stt.transcribe(audio)
    lat.stt_ms = (time.monotonic() - t0) * 1000
    transcript = result.text.strip()

    if not transcript:
        return "", history, lat

    _log.info("STT [%.0fms]: %s", lat.stt_ms, transcript)

    # Streaming agent → TTS worker thread → speaker
    tts = _get_tts()
    token_queue: q_mod.Queue[str | None] = q_mod.Queue()
    audio_queue: q_mod.Queue[Any] = q_mod.Queue()

    def tts_worker() -> None:
        def _gen():
            while True:
                tok = token_queue.get()
                if tok is None:
                    return
                yield tok
        try:
            for chunk in tts.stream(_gen()):
                audio_queue.put(chunk)
        except Exception as e:  # noqa: BLE001
            _log.error("TTS error: %s", e)
        finally:
            audio_queue.put(None)

    tts_thread = threading.Thread(target=tts_worker, daemon=True)
    tts_thread.start()

    reply_tokens: list[str] = []
    t_agent = time.monotonic()
    first_token = True

    async for token in voice_stream(transcript, history=history, tier=Tier.LOCAL):
        reply_tokens.append(token)
        if first_token:
            lat.agent_ttft_ms = (time.monotonic() - t_agent) * 1000
            first_token = False
        token_queue.put(token)
    token_queue.put(None)  # signal TTS to flush and stop

    reply = "".join(reply_tokens).strip()

    # Play audio chunks as they arrive; bail if TTS thread dies
    first_audio = True
    while True:
        try:
            chunk = audio_queue.get(timeout=10.0)
        except q_mod.Empty:
            _log.warning("TTS audio queue timeout — TTS thread may have crashed")
            break
        if chunk is None:
            break
        if first_audio:
            lat.tts_first_ms = (time.monotonic() - t_agent) * 1000
            lat.total_ms = (time.monotonic() - t_total) * 1000
            first_audio = False
        play_audio(chunk.audio, sample_rate=chunk.sample_rate, blocking=True)

    tts_thread.join(timeout=5.0)

    # Update history with this turn
    history.append({"role": "user", "content": transcript})
    history.append({"role": "assistant", "content": reply})

    # Persist this turn to long-term memory in the background (non-blocking).
    from friday.core.agent import persist_turn_async
    persist_turn_async(transcript, reply)

    _log.info("Turn done — total=%.0fms reply=%r", lat.total_ms, reply[:60])
    return reply, history, lat


async def push_to_talk_loop(history: list[dict[str, Any]] | None = None) -> None:
    """Hold SPACE to record, release to get a spoken answer."""
    from pynput import keyboard
    from rich.console import Console

    console = Console()
    vad = VAD(aggressiveness=2)

    if history is None:
        history = []

    console.print("[bold cyan]Sutra voice[/bold cyan] — hold [bold]SPACE[/bold] to speak  [bold]Ctrl+C[/bold] to exit")
    console.print("[dim]Pre-warming models…[/dim]", end="\r")
    await asyncio.get_event_loop().run_in_executor(None, prewarm)
    console.print("[dim]Ready.                [/dim]")

    stop_event = threading.Event()
    is_recording = threading.Event()

    def on_press(key: keyboard.Key) -> None:
        if key == keyboard.Key.space and not is_recording.is_set():
            is_recording.set()
            stop_event.clear()

    def on_release(key: keyboard.Key) -> None:
        if key == keyboard.Key.space:
            stop_event.set()

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()

    try:
        while True:
            is_recording.wait()
            is_recording.clear()
            console.print("[dim]● recording…[/dim]", end="\r")

            audio = record_while_held(stop_event)

            if len(audio) < 8000:
                console.print("[dim]too short, ignored[/dim]")
                continue

            console.print("[cyan]processing…  [/cyan]", end="\r")
            reply, history, lat = await process_audio(audio, history)

            if reply:
                _log_latency(VoiceTurn(
                    transcript="(ptt)",
                    reply_text=reply[:80],
                    latency=lat,
                ))
                console.print(
                    f"[dim]stt={lat.stt_ms:.0f}ms  "
                    f"ttft={lat.agent_ttft_ms:.0f}ms  "
                    f"tts={lat.tts_first_ms:.0f}ms  "
                    f"total={lat.total_ms:.0f}ms[/dim]"
                )

            await asyncio.sleep(0.05)

    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
        console.print("\n[dim]Voice mode off.[/dim]")


async def vad_listen_loop(history: list[dict[str, Any]] | None = None) -> None:
    """Continuous VAD mode — activates when speech is detected."""
    from rich.console import Console

    console = Console()
    # aggressiveness 3 = most aggressive non-speech rejection. At 2, ambient noise
    # reads as speech so end-of-turn never triggers and the mic stays open ~10-15s.
    # Override with VAD_AGGRESSIVENESS (0-3) for very quiet rooms.
    vad = VAD(aggressiveness=int(os.getenv("VAD_AGGRESSIVENESS", "3")))

    if history is None:
        history = []

    console.print("[bold cyan]Sutra voice (VAD)[/bold cyan] — listening  [bold]Ctrl+C[/bold] to exit")
    console.print("[dim]Pre-warming models…[/dim]", end="\r")
    await asyncio.get_event_loop().run_in_executor(None, prewarm)
    console.print("[dim]Ready.            [/dim]")

    try:
        while True:
            console.print("[dim]listening…[/dim]", end="\r")
            audio = record_with_vad(vad_fn=vad, silence_threshold_s=0.25, max_duration_s=30.0)

            if len(audio) < 8000:
                await asyncio.sleep(0.1)
                continue

            console.print("[cyan]heard you…[/cyan]", end="\r")
            reply, history, lat = await process_audio(audio, history)

            if reply:
                _log_latency(VoiceTurn(
                    transcript="(vad)",
                    reply_text=reply[:80],
                    latency=lat,
                ))
                console.print(
                    f"[dim]stt={lat.stt_ms:.0f}ms  "
                    f"ttft={lat.agent_ttft_ms:.0f}ms  "
                    f"tts={lat.tts_first_ms:.0f}ms  "
                    f"total={lat.total_ms:.0f}ms[/dim]"
                )

            await asyncio.sleep(0.05)

    except KeyboardInterrupt:
        console.print("\n[dim]Voice mode off.[/dim]")
