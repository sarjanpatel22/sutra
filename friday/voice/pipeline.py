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

from friday.voice.audio import (
    BargeInDetector,
    play_audio,
    play_chunks_interruptible,
    record_while_held,
    record_with_vad,
    record_with_vad_streaming,
)
from friday.voice.vad import SileroVAD

_log = logging.getLogger("sutra.voice")
_LATENCY_LOG = Path.home() / ".local" / "share" / "friday" / "voice_latency.jsonl"

_stt_instance = None
_tts_instance = None
_barge_vad_instance = None


def _get_barge_vad() -> SileroVAD:
    """Dedicated Silero VAD for barge-in detection (separate from the capture VAD).

    Kept apart so its rolling LSTM state never collides with the end-of-turn
    capture VAD. barge-in reads raw .prob(); the detector owns the threshold.
    """
    global _barge_vad_instance
    if _barge_vad_instance is None:
        _barge_vad_instance = SileroVAD()
    return _barge_vad_instance


def _barge_in_enabled() -> bool:
    """VAD-mode barge-in toggle. Off by default — see VOICE_BARGE_IN in .env.example.

    Opt-in because on open speakers (no echo cancellation) Sutra's own TTS bleeds
    into the mic and can self-interrupt. With a headset/close mic it's reliable.
    """
    return os.getenv("VOICE_BARGE_IN", "0").strip().lower() not in ("", "0", "false", "no")


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


def prewarm_models() -> None:
    """Load the heavy *sync* models (STT, TTS). Safe to run in an executor thread."""
    import numpy as np
    _log.info("pre-warming STT...")
    _get_stt().transcribe(np.zeros(16000, dtype=np.int16))
    _log.info("pre-warming TTS...")
    _get_tts().synthesize("warm")


async def prewarm_async() -> None:
    """Warm the *async* clients (memory store + local LLM) on the CURRENT loop.

    This MUST run on the same event loop the voice turns use. Graphiti's FalkorDB
    driver and litellm's async HTTP client bind their connection pool to the loop
    of the first await; warming them on a throwaway loop (e.g. an executor thread's
    asyncio.run) leaves the real turns hitting a closed loop — recall then dies with
    "Event loop is closed" and the LLM is effectively cold (TTFT ~3s, not ~450ms).
    """
    _log.info("pre-warming memory + local LLM...")
    try:
        from friday.core.agent import voice_stream
        from friday.core.router import Tier
        from friday.memory.store import get_store

        # Also warms the memory embedder (recall runs each voice turn).
        await get_store().recall("warmup", limit=1)
        async for _ in voice_stream("hi", history=[], tier=Tier.LOCAL):
            break  # first token means the model is resident in VRAM
    except Exception as e:  # noqa: BLE001 — Ollama/FalkorDB down: voice still works, just cold
        _log.warning("async pre-warm skipped (%s)", e)


async def prewarm() -> None:
    """Full pre-warm: heavy sync models off-loop, async clients on this loop.

    Without it the first turn pays a ~3s Ollama model-load on top of inference.
    """
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, prewarm_models)
    await prewarm_async()
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
    *,
    barge_in: bool = False,
    stt_result: Any = None,
) -> tuple[str, list[dict[str, Any]], TurnLatency, bool]:
    """STT → streaming agent → streaming TTS → play.

    Returns (reply_text, updated_history, latency, interrupted).
    history is extended in-place and also returned.

    barge_in: listen on the mic during playback and stop speaking the moment the
    user talks over Sutra (VAD-mode only; the LLM has already finished generating
    by the time playback starts, so only speech is cut, not the answer).
    """
    from friday.core.agent import (
        cloud_tools_available,
        likely_needs_tool,
        voice_agent_run,
        voice_stream,
    )
    from friday.core.router import Tier

    lat = TurnLatency()
    t_total = time.monotonic()

    # STT — use the streaming pre-computed result if the recorder already produced
    # one (transcription overlapped the silence wait), else transcribe now.
    stt = _get_stt()
    t0 = time.monotonic()
    result = stt_result if stt_result is not None else stt.transcribe(audio)
    lat.stt_ms = (time.monotonic() - t0) * 1000
    transcript = result.text.strip()

    if not transcript:
        return "", history, lat, False

    _log.info("STT [%.0fms]: %s", lat.stt_ms, transcript)

    # Skill-needing turns ("what's the weather", "how much RAM") go through cloud
    # function-calling; chit-chat stays on the fast local path.
    use_tools = likely_needs_tool(transcript) and cloud_tools_available()

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
    sample_rate = tts.sample_rate
    first_audio = True
    interrupted = False

    def next_chunk() -> np.ndarray | None:
        nonlocal first_audio
        try:
            chunk = audio_queue.get(timeout=10.0)
        except q_mod.Empty:
            _log.warning("TTS audio queue timeout — TTS thread may have crashed")
            return None
        if chunk is None:
            return None
        if first_audio:
            lat.tts_first_ms = (time.monotonic() - t_agent) * 1000
            lat.total_ms = (time.monotonic() - t_total) * 1000
            first_audio = False
        return chunk.audio

    def play_blocking() -> None:
        while True:
            ch = next_chunk()
            if ch is None:
                break
            play_audio(ch, sample_rate=sample_rate, blocking=True)

    async def generate_local() -> None:
        nonlocal first_token
        async for token in voice_stream(transcript, history=history, tier=Tier.LOCAL):
            reply_tokens.append(token)
            if first_token:
                lat.agent_ttft_ms = (time.monotonic() - t_agent) * 1000
                first_token = False
            token_queue.put(token)
        token_queue.put(None)  # signal TTS to flush and stop

    if use_tools:
        # Cloud function-calling: tool calls need the whole response, so this isn't
        # token-streamed. Synthesize the finished reply, then speak it. Fall back to
        # the local path if the cloud round-trip fails (offline / key issue).
        try:
            reply_text = await voice_agent_run(transcript, history=history)
            lat.agent_ttft_ms = (time.monotonic() - t_agent) * 1000
            reply_tokens = [reply_text]
            token_queue.put(reply_text)
            token_queue.put(None)
            play_blocking()
        except Exception as e:  # noqa: BLE001
            _log.warning("voice tool path failed (%s) — falling back to local", e)
            use_tools = False

    if not use_tools:
        if barge_in:
            # Barge-in's grace window must start at first audio, so generate fully,
            # then play interruptibly.
            await generate_local()
            interrupted = play_chunks_interruptible(
                next_chunk,
                vad=_get_barge_vad(),
                sample_rate=sample_rate,
                detector=BargeInDetector.from_env(),
            )
            if interrupted:
                _log.info("barge-in — stopped speaking mid-reply")
        else:
            # Overlap: speak the first sentence while the rest still generates —
            # cuts the dead air between answer-ready and audio-out.
            playback_thread = threading.Thread(target=play_blocking, daemon=True)
            playback_thread.start()
            await generate_local()
            playback_thread.join(timeout=30.0)

    reply = "".join(reply_tokens).strip()
    tts_thread.join(timeout=5.0)

    # Update history with this turn
    history.append({"role": "user", "content": transcript})
    history.append({"role": "assistant", "content": reply})

    # Persist this turn to long-term memory in the background (non-blocking) — but
    # keep low-confidence far-field/hallucinated transcripts out of the graph.
    min_logprob = float(os.getenv("STT_MIN_LOGPROB", "-1.0"))
    max_nospeech = float(os.getenv("STT_MAX_NOSPEECH", "0.6"))
    if result.avg_logprob >= min_logprob and result.no_speech_prob <= max_nospeech:
        from friday.core.agent import persist_turn_async
        persist_turn_async(transcript, reply)
    else:
        _log.info(
            "skip persist — low STT confidence (logprob=%.2f no_speech=%.2f): %r",
            result.avg_logprob, result.no_speech_prob, transcript[:40],
        )

    _log.info("Turn done — total=%.0fms reply=%r", lat.total_ms, reply[:60])
    return reply, history, lat, interrupted


async def push_to_talk_loop(history: list[dict[str, Any]] | None = None) -> None:
    """Hold SPACE to record, release to get a spoken answer."""
    from pynput import keyboard
    from rich.console import Console

    console = Console()
    # push-to-talk gates recording by the key, so it needs no VAD.

    if history is None:
        history = []

    console.print("[bold cyan]Sutra voice[/bold cyan] — hold [bold]SPACE[/bold] to speak  [bold]Ctrl+C[/bold] to exit")
    console.print("[dim]Pre-warming models…[/dim]", end="\r")
    await prewarm()
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
            reply, history, lat, _ = await process_audio(audio, history)

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
    # Silero neural VAD — noise-robust, doesn't fragment speech like webrtcvad did.
    # VAD_THRESHOLD (0–1) tunes sensitivity; 0.5 is the default.
    vad = SileroVAD(threshold=float(os.getenv("VAD_THRESHOLD", "0.5")))

    if history is None:
        history = []

    barge_in = _barge_in_enabled()
    streaming_stt = os.getenv("VOICE_STREAMING_STT", "0").strip().lower() not in (
        "", "0", "false", "no"
    )

    console.print("[bold cyan]Sutra voice (VAD)[/bold cyan] — listening  [bold]Ctrl+C[/bold] to exit")
    if barge_in:
        console.print("[dim]barge-in on — talk over her to interrupt[/dim]")
    if streaming_stt:
        console.print("[dim]streaming STT on — transcribing as you speak[/dim]")
    console.print("[dim]Pre-warming models…[/dim]", end="\r")
    await prewarm()
    console.print("[dim]Ready.            [/dim]")

    try:
        while True:
            console.print("[dim]listening…[/dim]", end="\r")
            # End-of-turn silence: the single biggest warm-latency knob. 0.5s (LiveKit's
            # default) waits for a real pause; lower = snappier but risks cutting pauses.
            silence_s = float(os.getenv("VAD_SILENCE_S", "0.5"))
            if streaming_stt:
                # Transcribe during the silence wait — STT off the critical path.
                audio, stt_result = record_with_vad_streaming(
                    vad,
                    lambda clip: _get_stt().transcribe(clip),
                    silence_threshold_s=silence_s,
                    max_duration_s=30.0,
                )
            else:
                audio = record_with_vad(
                    vad_fn=vad, silence_threshold_s=silence_s, max_duration_s=30.0
                )
                stt_result = None

            if len(audio) < 8000:
                await asyncio.sleep(0.1)
                continue

            console.print("[cyan]heard you…[/cyan]", end="\r")
            reply, history, lat, interrupted = await process_audio(
                audio, history, barge_in=barge_in, stt_result=stt_result
            )
            if interrupted:
                console.print("[yellow]⊘ barge-in — stopped speaking[/yellow]")

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
