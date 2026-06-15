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

from friday.gui.orb_client import push as orb_push
from friday.voice import turn_detector
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


async def _connect_mcp(console: Any) -> None:
    """Connect configured MCP servers so their tools join the voice agent's skills.
    No-op when none configured; never raises into the voice loop."""
    try:
        from friday.mcp.bridge import get_bridge
        bridge = await get_bridge()
        if bridge and bridge.tool_names:
            console.print(
                f"[dim]MCP: {len(bridge.tool_names)} tool(s) from "
                f"{', '.join(bridge.servers)}[/dim]"
            )
    except Exception as e:  # noqa: BLE001 — MCP must never block voice
        console.print(f"[yellow]MCP connect skipped ({e})[/yellow]")


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


# Orb voice-reactivity tuning. Kokoro streams coarse (~sentence) chunks, so a
# single RMS per chunk would flatline the orb; instead we trace a short-time RMS
# envelope. 40ms window ≈ 25 amplitude updates/sec; *7 maps Kokoro speech RMS to a
# full 0..1 pulse (calibrated against real TTS: p95→1.0, silences→<0.1).
_ORB_ENV_WIN_S = 0.04
_ORB_AMP_SCALE = 7.0


def _chunk_envelope(chunk: np.ndarray, sample_rate: int) -> list[float]:
    """Short-time RMS envelope of a TTS chunk, scaled to 0..1 orb amplitudes."""
    a = chunk.astype(np.float64)
    if np.issubdtype(chunk.dtype, np.integer):
        a /= 32768.0  # normalise int16 PCM to [-1, 1]
    win = max(1, int(sample_rate * _ORB_ENV_WIN_S))
    return [
        min(1.0, float(np.sqrt(np.mean(np.square(a[i:i + win])))) * _ORB_AMP_SCALE)
        for i in range(0, max(1, len(a) - win), win)
    ]


def _orb_envelope_worker(env: list[float]) -> None:
    """Walk an amplitude envelope in real time so the orb moves with the speech."""
    for amp in env:
        orb_push("speaking", amp)
        time.sleep(_ORB_ENV_WIN_S)


def _drive_orb_speaking(chunk: np.ndarray, sample_rate: int) -> None:
    """Trace this chunk's loudness envelope onto the orb, paced to its playback.

    Best-effort and decoupled: a daemon thread pushes the envelope while the chunk
    plays, so the audio path never blocks and nothing breaks if no orb is running.
    """
    try:
        if chunk is None or len(chunk) == 0:
            return
        env = _chunk_envelope(chunk, sample_rate)
        if env:
            threading.Thread(target=_orb_envelope_worker, args=(env,), daemon=True).start()
    except Exception:  # noqa: BLE001 — orb reactivity must never break playback
        pass


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

    # One brain decides: act (cloud function-calling) vs chit-chat (fast local path),
    # and at what effort. A real task — "go to tryhackme and start a room" — now goes
    # agentic even without a keyword match; plain talk stays local.
    from friday.core.autoroute import decide

    plan = decide(transcript)
    # Deterministic fast-path: unambiguous commands ("open Firefox", "close X",
    # "switch to Y") run the skill DIRECTLY — no LLM, so they work instantly even
    # when the free cloud tiers are rate-limited (the old silent-failure cause).
    command_result = None
    if plan.act:
        from friday.core.commands import try_command
        command_result = await try_command(transcript)
        if command_result is not None:
            _log.info("voice command (no LLM): %s -> %s",
                      command_result.get("skill"), command_result.get("result"))
    use_tools = command_result is None and plan.act and cloud_tools_available()
    if plan.act and command_result is None:
        _log.info("voice route: %s effort (%s) — %s", plan.effort, plan.tier.value, plan.reason)

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
        _drive_orb_speaking(chunk.audio, sample_rate)  # trace loudness onto the orb
        return chunk.audio

    def play_blocking() -> None:
        while True:
            ch = next_chunk()
            if ch is None:
                break
            play_audio(ch, sample_rate=sample_rate, blocking=True)

    # Substantive chat deserves the free 70B (Tier.MEDIUM); trivial greetings stay on
    # the instant local 4B. Degrade to LOCAL when no free cloud key is set so offline
    # still answers. voice_stream still escalates to fallback_tier on garbage output.
    from friday.core.autoroute import chat_tier
    chat_t = chat_tier(transcript) if cloud_tools_available() else Tier.LOCAL

    async def generate_local() -> None:
        nonlocal first_token
        async for token in voice_stream(transcript, history=history, tier=chat_t):
            reply_tokens.append(token)
            if first_token:
                lat.agent_ttft_ms = (time.monotonic() - t_agent) * 1000
                first_token = False
            token_queue.put(token)
        token_queue.put(None)  # signal TTS to flush and stop

    if command_result is not None:
        # The skill already ran deterministically — just speak the confirmation.
        reply_text = command_result["spoken"]
        lat.agent_ttft_ms = (time.monotonic() - t_agent) * 1000
        reply_tokens = [reply_text]
        token_queue.put(reply_text)
        token_queue.put(None)
        play_blocking()
    elif use_tools:
        # Cloud function-calling: tool calls need the whole response, so this isn't
        # token-streamed. Synthesize the finished reply, then speak it. Fall back to
        # the local path if the cloud round-trip fails (offline / key issue).
        try:
            reply_text = await voice_agent_run(
                transcript, history=history, tier=plan.tier,
                force_tool=plan.tool_required,
            )
            lat.agent_ttft_ms = (time.monotonic() - t_agent) * 1000
            reply_tokens = [reply_text]
            token_queue.put(reply_text)
            token_queue.put(None)
            play_blocking()
        except Exception as e:  # noqa: BLE001
            _log.warning("voice tool path failed (%s) — falling back to local", e)
            use_tools = False

    if command_result is None and not use_tools:
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


async def process_text(text: str, history: list[dict[str, Any]]) -> str:
    """Typed input → streaming agent → streaming TTS → speak + drive the orb.

    The mic-free twin of process_audio: the same agent/TTS/orb plumbing, minus
    STT/VAD/barge-in. Powers the text-presence loop (`sutra voice --mode text`) so
    the full spoken + on-screen presence works before a headset arrives — type and
    Sutra answers aloud while the orb pulses to her own voice. history is extended
    in place; returns the reply text.
    """
    from friday.core.agent import (
        cloud_tools_available,
        persist_turn_async,
        voice_agent_run,
        voice_stream,
    )
    from friday.core.router import Tier

    text = text.strip()
    if not text:
        return ""

    from friday.core.autoroute import decide

    plan = decide(text)
    command_result = None
    if plan.act:
        from friday.core.commands import try_command
        command_result = await try_command(text)
    use_tools = command_result is None and plan.act and cloud_tools_available()
    tts = _get_tts()
    sample_rate = tts.sample_rate
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

    def play_blocking() -> None:
        while True:
            try:
                chunk = audio_queue.get(timeout=10.0)
            except q_mod.Empty:
                _log.warning("TTS audio queue timeout — TTS thread may have crashed")
                return
            if chunk is None:
                return
            _drive_orb_speaking(chunk.audio, sample_rate)  # trace loudness onto the orb
            play_audio(chunk.audio, sample_rate=sample_rate, blocking=True)

    reply_tokens: list[str] = []
    if command_result is not None:
        # Deterministic command already ran the skill — just speak the confirmation.
        reply_text = command_result["spoken"]
        reply_tokens = [reply_text]
        token_queue.put(reply_text)
        token_queue.put(None)
        play_blocking()
    elif use_tools:
        # Cloud function-calling needs the whole reply (not token-streamed); fall back
        # to the local streaming path if the cloud round-trip fails (offline/key).
        try:
            reply_text = await voice_agent_run(
                text, history=history, tier=plan.tier, force_tool=plan.tool_required
            )
            reply_tokens = [reply_text]
            token_queue.put(reply_text)
            token_queue.put(None)
            play_blocking()
        except Exception as e:  # noqa: BLE001
            _log.warning("text tool path failed (%s) — falling back to local", e)
            use_tools = False

    if command_result is None and not use_tools:
        # Substantive chat → free 70B; trivial social turns stay on the instant local
        # model. Degrade to LOCAL with no free cloud key so offline still works.
        from friday.core.autoroute import chat_tier
        chat_t = chat_tier(text) if cloud_tools_available() else Tier.LOCAL
        # Overlap: speak the first sentence while the rest still generates.
        playback_thread = threading.Thread(target=play_blocking, daemon=True)
        playback_thread.start()
        async for token in voice_stream(text, history=history, tier=chat_t):
            reply_tokens.append(token)
            token_queue.put(token)
        token_queue.put(None)
        playback_thread.join(timeout=30.0)

    reply = "".join(reply_tokens).strip()
    tts_thread.join(timeout=5.0)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    persist_turn_async(text, reply)  # typed input is always high-confidence
    return reply


async def push_to_talk_loop(history: list[dict[str, Any]] | None = None) -> None:
    """Hold SPACE to record, release to get a spoken answer."""
    from pynput import keyboard
    from rich.console import Console

    console = Console()
    # push-to-talk gates recording by the key, so it needs no VAD.

    if history is None:
        history = []

    console.print(
        "[bold cyan]Sutra voice[/bold cyan] — hold [bold]SPACE[/bold] to speak  "
        "[bold]Ctrl+C[/bold] to exit"
    )
    console.print("[dim]Pre-warming models…[/dim]", end="\r")
    await prewarm()
    await _connect_mcp(console)
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
                # process_audio appends (user transcript, assistant reply) to history,
                # so the real recognized text is the just-added user turn — log that,
                # not a "(ptt)" placeholder, so the latency log shows what Sutra heard.
                transcript = history[-2]["content"] if len(history) >= 2 else ""
                _log_latency(VoiceTurn(
                    transcript=transcript,
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
        from friday.mcp.bridge import close_bridge
        await close_bridge()
        console.print("\n[dim]Voice mode off.[/dim]")


async def text_presence_loop(history: list[dict[str, Any]] | None = None) -> None:
    """Type to Sutra; she answers aloud while the orb pulses. Mic-free presence.

    The full spoken + visual experience without a headset: read a typed line, run
    the same agent→TTS→orb path as the voice loops, show the reply, repeat. Pair
    with `--orb` for the on-screen presence. Ctrl+C, EOF, or 'exit' to leave.
    """
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    if history is None:
        history = []

    console.print(
        "[bold cyan]Sutra presence[/bold cyan] (text) — type to her, she answers "
        "aloud  [bold]Ctrl+C[/bold] to exit"
    )
    console.print("[dim]Pre-warming models…[/dim]", end="\r")
    await prewarm()
    await _connect_mcp(console)
    console.print("[dim]Ready.                [/dim]")
    orb_push("idle")

    try:
        while True:
            orb_push("listening")  # presence settles to its calm listening state
            try:
                text = (await asyncio.to_thread(console.input, "[bold]you ›[/bold] ")).strip()
            except EOFError:
                break
            if not text:
                continue
            if text.lower() in ("exit", "quit", ":q"):
                break
            console.print("[cyan]thinking…[/cyan]", end="\r")
            reply = await process_text(text, history)
            if reply:
                console.print(Panel(reply, title="sutra", border_style="cyan", expand=False))
            await asyncio.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        orb_push("idle")  # reset the orb when the presence loop exits
        from friday.mcp.bridge import close_bridge
        await close_bridge()
        console.print("\n[dim]Presence off.[/dim]")


def _utterance_complete(stt_result: Any) -> bool:
    """Semantic end-of-turn check on an STT result (drives VAD endpointing)."""
    return turn_detector.is_complete_utterance(getattr(stt_result, "text", "") or "")


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
    semantic_turn = os.getenv("VOICE_SEMANTIC_TURN", "1").strip().lower() not in (
        "", "0", "false", "no"
    )
    max_silence_s = float(os.getenv("VAD_MAX_SILENCE_S", "2.0"))

    console.print(
        "[bold cyan]Sutra voice (VAD)[/bold cyan] — listening  [bold]Ctrl+C[/bold] to exit"
    )
    if barge_in:
        console.print("[dim]barge-in on — talk over her to interrupt[/dim]")
    if streaming_stt:
        console.print("[dim]streaming STT on — transcribing as you speak[/dim]")
    if semantic_turn:
        console.print("[dim]semantic turn-taking on — waits through mid-sentence pauses[/dim]")
    console.print("[dim]Pre-warming models…[/dim]", end="\r")
    await prewarm()
    await _connect_mcp(console)
    console.print("[dim]Ready.            [/dim]")

    try:
        while True:
            console.print("[dim]listening…[/dim]", end="\r")
            orb_push("listening")  # orb settles to its calm listening state between turns
            # End-of-turn silence: the single biggest knob for not cutting Sarjan off.
            # 0.5s tripped on mid-sentence pauses; 0.8s tolerates a natural breath
            # (production turn-taking uses 0.5–0.8s of silence, the rest is semantic).
            # Lower = snappier but risks interrupting; higher = calmer but laggier.
            silence_s = float(os.getenv("VAD_SILENCE_S", "0.8"))
            if streaming_stt or semantic_turn:
                # Transcribe during the silence wait (STT off the critical path) and,
                # with semantic_turn, hold the turn open through mid-sentence pauses.
                audio, stt_result = record_with_vad_streaming(
                    vad,
                    lambda clip: _get_stt().transcribe(clip),
                    silence_threshold_s=silence_s,
                    max_silence_threshold_s=max_silence_s if semantic_turn else None,
                    is_complete_fn=_utterance_complete if semantic_turn else None,
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
                # process_audio appends (user transcript, assistant reply) to history,
                # so the real recognized text is the just-added user turn — log that,
                # not a "(vad)" placeholder, so the latency log shows what Sutra heard.
                transcript = history[-2]["content"] if len(history) >= 2 else ""
                _log_latency(VoiceTurn(
                    transcript=transcript,
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
        orb_push("idle")  # reset the orb when the voice loop exits
    finally:
        from friday.mcp.bridge import close_bridge
        await close_bridge()
        console.print("\n[dim]Voice mode off.[/dim]")


async def wake_listen_loop(history: list[dict[str, Any]] | None = None) -> None:
    """Wake-word mode — dormant until the wake word, then one VAD turn, then dormant.

    Opt-in (`sutra voice --mode wake`). Sits idle and silent until the wake word fires
    (openWakeWord on the existing onnxruntime), then runs exactly the normal VAD
    capture + process_audio turn and returns to dormant. PTT and plain VAD modes are
    untouched. The orb shows idle while dormant and listening once woken.
    """
    from rich.console import Console

    from friday.voice.audio import listen_for_wake, record_with_vad
    from friday.voice.wakeword import WakeWord, make_detector

    console = Console()
    vad = SileroVAD(threshold=float(os.getenv("VAD_THRESHOLD", "0.5")))
    if history is None:
        history = []
    phrase = os.getenv("WAKEWORD_MODEL", "hey_jarvis")

    console.print(
        f"[bold cyan]Sutra voice (wake)[/bold cyan] — say [bold]'{phrase}'[/bold] then talk  "
        "[bold]Ctrl+C[/bold] to exit"
    )
    console.print("[dim]Pre-warming…[/dim]", end="\r")
    await prewarm()
    ww = WakeWord()
    ww.warm()  # load the wake model now, not on the first frame
    detector = make_detector(score_fn=ww.score)
    await _connect_mcp(console)
    console.print("[dim]Ready — dormant. Say the wake word.[/dim]")

    try:
        while True:
            orb_push("idle")  # dormant presence until woken
            console.print("[dim]· dormant — say the wake word[/dim]", end="\r")
            if not listen_for_wake(detector):
                break  # stopped
            orb_push("listening")
            console.print("[green]✦ woke — listening…[/green]            ")
            silence_s = float(os.getenv("VAD_SILENCE_S", "0.8"))
            audio = record_with_vad(
                vad_fn=vad, silence_threshold_s=silence_s, max_duration_s=30.0
            )
            if len(audio) < 8000:
                continue
            console.print("[cyan]heard you…[/cyan]", end="\r")
            reply, history, lat, _ = await process_audio(audio, history)
            if reply:
                transcript = history[-2]["content"] if len(history) >= 2 else ""
                _log_latency(VoiceTurn(transcript=transcript, reply_text=reply[:80], latency=lat))
                console.print(
                    f"[dim]stt={lat.stt_ms:.0f}ms  ttft={lat.agent_ttft_ms:.0f}ms  "
                    f"tts={lat.tts_first_ms:.0f}ms  total={lat.total_ms:.0f}ms[/dim]"
                )
            await asyncio.sleep(0.05)
    except KeyboardInterrupt:
        orb_push("idle")
    finally:
        from friday.mcp.bridge import close_bridge
        await close_bridge()
        console.print("\n[dim]Voice mode off.[/dim]")
