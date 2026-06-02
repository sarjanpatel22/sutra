"""Voice pipeline: mic → VAD → Whisper STT → agent → Kokoro TTS → speaker.

Two modes:
  push_to_talk()  — hold spacebar to record, release to process
  vad_listen()    — continuous VAD, auto-detects speech start/end

Latency is measured and logged at every stage.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from friday.voice.audio import play_audio, record_with_vad, record_while_held
from friday.voice.vad import VAD

_log = logging.getLogger("sutra.voice")
_LATENCY_LOG = Path.home() / ".local" / "share" / "friday" / "voice_latency.jsonl"


@dataclass
class TurnLatency:
    """Latency breakdown for one voice turn."""
    vad_ms: float = 0.0         # time from speech-start to end-of-speech detection
    stt_ms: float = 0.0         # Whisper transcription time
    agent_ttft_ms: float = 0.0  # agent time-to-first-token
    tts_first_ms: float = 0.0   # TTS first audio chunk
    total_ms: float = 0.0       # wall time: speech-end → first audio


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
            "vad_ms": round(turn.latency.vad_ms),
            "stt_ms": round(turn.latency.stt_ms),
            "agent_ttft_ms": round(turn.latency.agent_ttft_ms),
            "tts_first_ms": round(turn.latency.tts_first_ms),
            "total_ms": round(turn.latency.total_ms),
        }) + "\n")


async def process_audio(
    audio: np.ndarray,
    history: list[dict],
    *,
    stt_latency_hint: float = 0.0,
) -> tuple[str, list[dict], TurnLatency]:
    """STT → agent → streaming TTS → play. Returns (reply, updated_history, latency)."""
    from friday.core.agent import run
    from friday.core.router import Tier
    from friday.voice.audio import play_audio
    from friday.voice.stt import get_stt
    from friday.voice.tts import get_tts

    lat = TurnLatency(stt_ms=stt_latency_hint)
    t_total_start = time.monotonic()

    # STT
    t0 = time.monotonic()
    stt = get_stt()
    result = stt.transcribe(audio)
    lat.stt_ms = (time.monotonic() - t0) * 1000
    transcript = result.text

    if not transcript.strip():
        return "", history, lat

    _log.info("STT [%.0fms]: %s", lat.stt_ms, transcript)

    # Agent (use LOCAL tier first — Gemma via Ollama)
    t0 = time.monotonic()
    reply, history = await run(transcript, history=history, tier=Tier.LOCAL)
    lat.agent_ttft_ms = (time.monotonic() - t0) * 1000

    _log.info("Agent [%.0fms]: %s", lat.agent_ttft_ms, reply[:80])

    # TTS + streaming playback
    tts = get_tts()
    t0 = time.monotonic()
    first_chunk = True
    for chunk in tts.stream(iter(reply.split())):
        if first_chunk:
            lat.tts_first_ms = (time.monotonic() - t0) * 1000
            lat.total_ms = (time.monotonic() - t_total_start) * 1000
            first_chunk = False
        play_audio(chunk.audio, sample_rate=chunk.sample_rate, blocking=True)

    return reply, history, lat


async def push_to_talk_loop(history: list[dict] | None = None) -> None:
    """Hold SPACE to record, release to get a spoken answer.

    Runs until KeyboardInterrupt.
    """
    from pynput import keyboard
    from rich.console import Console

    console = Console()
    vad = VAD(aggressiveness=2)

    if history is None:
        history = []

    console.print("[bold cyan]Voice mode[/bold cyan] — hold [bold]SPACE[/bold] to speak, [bold]Ctrl+C[/bold] to exit")

    stop_event = threading.Event()
    is_recording = threading.Event()

    def on_press(key: keyboard.Key) -> None:
        if key == keyboard.Key.space and not is_recording.is_set():
            is_recording.set()
            stop_event.clear()
            console.print("[dim]● recording…[/dim]", end="\r")

    def on_release(key: keyboard.Key) -> None:
        if key == keyboard.Key.space:
            stop_event.set()

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()

    try:
        while True:
            is_recording.wait()
            is_recording.clear()

            t_speech_end = time.monotonic()
            audio = record_while_held(stop_event)
            vad_ms = (time.monotonic() - t_speech_end) * 1000

            if len(audio) < 8000:  # < 0.5s — too short, ignore
                console.print("[dim]too short, ignored[/dim]")
                continue

            console.print("[cyan]processing…[/cyan]", end="\r")
            reply, history, lat = await process_audio(
                audio, history, stt_latency_hint=vad_ms
            )

            if reply:
                _log_latency(VoiceTurn(
                    transcript="(ptt)",
                    reply_text=reply[:80],
                    latency=lat,
                ))
                console.print(
                    f"[dim]stt={lat.stt_ms:.0f}ms  "
                    f"agent={lat.agent_ttft_ms:.0f}ms  "
                    f"tts={lat.tts_first_ms:.0f}ms  "
                    f"total={lat.total_ms:.0f}ms[/dim]"
                )

            await asyncio.sleep(0.05)

    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
        console.print("\n[dim]Voice mode off.[/dim]")


async def vad_listen_loop(history: list[dict] | None = None) -> None:
    """Continuous VAD mode — speaks automatically when silence detected.

    More natural but requires clean mic environment.
    """
    from rich.console import Console

    console = Console()
    vad = VAD(aggressiveness=2)

    if history is None:
        history = []

    console.print(
        "[bold cyan]Voice mode (VAD)[/bold cyan] — listening… [bold]Ctrl+C[/bold] to exit"
    )

    try:
        while True:
            console.print("[dim]listening…[/dim]", end="\r")
            t0 = time.monotonic()
            audio = record_with_vad(
                vad_fn=vad,
                silence_threshold_s=0.35,
                max_duration_s=30.0,
            )
            vad_ms = (time.monotonic() - t0) * 1000

            if len(audio) < 8000:
                await asyncio.sleep(0.1)
                continue

            console.print("[cyan]heard you, processing…[/cyan]", end="\r")
            reply, history, lat = await process_audio(audio, history, stt_latency_hint=vad_ms)

            if reply:
                _log_latency(VoiceTurn(
                    transcript="(vad)",
                    reply_text=reply[:80],
                    latency=lat,
                ))
                console.print(
                    f"[dim]vad={lat.vad_ms:.0f}ms  stt={lat.stt_ms:.0f}ms  "
                    f"agent={lat.agent_ttft_ms:.0f}ms  "
                    f"tts={lat.tts_first_ms:.0f}ms  "
                    f"total={lat.total_ms:.0f}ms[/dim]"
                )

            await asyncio.sleep(0.05)

    except KeyboardInterrupt:
        console.print("\n[dim]Voice mode off.[/dim]")
