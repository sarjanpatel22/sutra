"""Audio I/O helpers — mic recording and speaker playback via sounddevice."""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import sounddevice as sd

from friday.voice import turn_detector

_log = logging.getLogger("sutra.voice.audio")

SAMPLE_RATE = 16000   # Hz — Whisper native
CHANNELS = 1
DTYPE = np.int16
BLOCK_SIZE = 512      # 32ms at 16kHz — Silero VAD's required chunk size

# Set AUDIO_INPUT_DEVICE / AUDIO_OUTPUT_DEVICE in .env to override.
# "pulse" routes through PipeWire/PulseAudio, which resamples to/from the device's
# native rate — raw ALSA devices often reject 16kHz outright (PaErrorCode -9997).
_INPUT_DEVICE: str | int | None = os.getenv("AUDIO_INPUT_DEVICE", "pulse") or None
_OUTPUT_DEVICE: str | int | None = os.getenv("AUDIO_OUTPUT_DEVICE", "pulse") or None

# Convert numeric strings to int
if isinstance(_INPUT_DEVICE, str) and _INPUT_DEVICE.isdigit():
    _INPUT_DEVICE = int(_INPUT_DEVICE)
if isinstance(_OUTPUT_DEVICE, str) and _OUTPUT_DEVICE.isdigit():
    _OUTPUT_DEVICE = int(_OUTPUT_DEVICE)


def list_devices() -> list[dict[str, object]]:
    devices = []
    for i, d in enumerate(sd.query_devices()):
        devices.append({
            "index": i,
            "name": d["name"],
            "inputs": d["max_input_channels"],
            "outputs": d["max_output_channels"],
            "default_sr": d["default_samplerate"],
        })
    return devices


def default_input_device() -> int:
    return sd.default.device[0]  # type: ignore[index]


def default_output_device() -> int:
    return sd.default.device[1]  # type: ignore[index]


def record_while_held(stop_event: threading.Event, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Record audio continuously until stop_event is set. Returns int16 array."""
    chunks: list[np.ndarray] = []

    def callback(indata: np.ndarray, frames: int, time_info: object, status: object) -> None:
        chunks.append(indata[:, 0].copy())

    with sd.InputStream(
        device=_INPUT_DEVICE,
        samplerate=sample_rate,
        channels=CHANNELS,
        dtype=DTYPE,
        blocksize=BLOCK_SIZE,
        callback=callback,
    ):
        stop_event.wait()

    return np.concatenate(chunks) if chunks else np.array([], dtype=DTYPE)


def _speech_window_start(first_speech_idx: int | None, pad_frames: int) -> int | None:
    """Index to start the captured clip: pad_frames before first speech, clamped.

    Returns None if no speech was ever detected.
    """
    if first_speech_idx is None:
        return None
    return max(0, first_speech_idx - pad_frames)


def _frame_rms(frame: np.ndarray) -> float:
    """RMS amplitude of an int16 PCM frame (0 for an empty frame)."""
    f = frame.astype(np.float32)
    return float(np.sqrt(np.mean(f * f))) if f.size else 0.0


def _is_speech_frame(
    vad_flag: bool,
    rms: float,
    noise_floor: float,
    *,
    margin: float = 2.5,
    min_rms: float = 250.0,
) -> bool:
    """webrtcvad + energy gate: a frame is speech only if BOTH agree.

    webrtcvad alone flags steady room noise as "speech", so end-of-turn never
    fires and the mic stays open for seconds. Require the frame to also be louder
    than the calibrated ambient floor (× margin) and a small absolute minimum, so
    that once the speaker goes quiet the silence actually registers.
    """
    return vad_flag and rms > max(min_rms, noise_floor * margin)


@dataclass
class BargeInDetector:
    """Decide when the user has started talking over Sutra's TTS (barge-in).

    Fed one mic frame at a time *while she is speaking*; returns True the moment
    it's confident the user is interrupting. Every guard exists to reject the
    speaker→mic echo path (Sutra hearing her own TTS) without a hardware acoustic
    echo canceller:

      grace_frames    ignore the mic while the output settles, so the first TTS
                      chunk can't self-trigger.
      threshold       Silero speech-prob cutoff, raised above the 0.5 capture
                      default — echo bleed gives weak/fluctuating prob, a real
                      interruption is confident.
      rms_floor       absolute energy gate — open-speaker echo is attenuated;
                      require real signal.
      trigger_frames  consecutive qualifying frames before firing — rejects a
                      lone echoed phoneme, demands sustained speech.

    With headphones or a close mic (no echo path) real speech clears all four
    trivially; they only earn their keep on open-speaker setups. Frames are
    BLOCK_SIZE @ SAMPLE_RATE (~32 ms), so the defaults ≈ 256 ms grace / 192 ms
    sustained speech.
    """

    threshold: float = 0.7
    rms_floor: float = 500.0
    grace_frames: int = 8
    trigger_frames: int = 6
    _elapsed: int = field(default=0, init=False, repr=False)
    _streak: int = field(default=0, init=False, repr=False)

    @classmethod
    def from_env(cls) -> BargeInDetector:
        """Build a detector from VOICE_BARGE_* env knobs (grace/sustain in seconds)."""
        fps = SAMPLE_RATE / BLOCK_SIZE  # ~31.25 frames/s
        return cls(
            threshold=float(os.getenv("VOICE_BARGE_THRESHOLD", "0.7")),
            rms_floor=float(os.getenv("VOICE_BARGE_RMS_FLOOR", "500")),
            grace_frames=max(1, round(float(os.getenv("VOICE_BARGE_GRACE_S", "0.25")) * fps)),
            trigger_frames=max(1, round(float(os.getenv("VOICE_BARGE_SUSTAIN_S", "0.2")) * fps)),
        )

    def reset(self) -> None:
        """Clear counters — call at the start of each playback."""
        self._elapsed = 0
        self._streak = 0

    def feed(self, speech_prob: float, rms: float) -> bool:
        """Consume one mic frame; return True once a barge-in is detected."""
        self._elapsed += 1
        if self._elapsed <= self.grace_frames:
            return False
        if speech_prob >= self.threshold and rms >= self.rms_floor:
            self._streak += 1
        else:
            self._streak = 0
        return self._streak >= self.trigger_frames


def record_with_vad(
    vad_fn: Callable[[bytes], bool],
    *,
    sample_rate: int = SAMPLE_RATE,
    silence_threshold_s: float = 0.5,
    max_duration_s: float = 30.0,
    pre_speech_pad_s: float = 0.3,
    energy_gate: bool = False,
) -> np.ndarray:
    """Record until VAD detects end-of-speech (silence_threshold_s of silence).

    Captures from pad_frames before the first detected speech through end-of-speech.
    Returns int16 PCM array ready for Whisper.

    energy_gate: layer an RMS noise-floor gate on top of vad_fn. Needed for the
    legacy webrtcvad (it flags room noise as speech); leave OFF for SileroVAD,
    which is already noise-robust — gating it would clip quiet speech. If vad_fn
    is stateful (SileroVAD), its .reset() is called so state doesn't leak between turns.
    """
    if hasattr(vad_fn, "reset"):
        vad_fn.reset()
    silence_frames = int(silence_threshold_s * sample_rate / BLOCK_SIZE)
    max_frames = int(max_duration_s * sample_rate / BLOCK_SIZE)
    pad_frames = int(pre_speech_pad_s * sample_rate / BLOCK_SIZE)

    audio_q: queue.Queue[np.ndarray] = queue.Queue()

    def callback(indata: np.ndarray, frames: int, time_info: object, status: object) -> None:
        audio_q.put(indata[:, 0].copy())

    all_frames: list[np.ndarray] = []
    consecutive_silence = 0
    speech_started = False
    first_speech_idx: int | None = None
    # Ambient noise floor, estimated from the quiet lead-in (energy_gate only).
    noise_samples: list[float] = []
    noise_floor = 0.0

    with sd.InputStream(
        device=_INPUT_DEVICE,
        samplerate=sample_rate,
        channels=CHANNELS,
        dtype=DTYPE,
        blocksize=BLOCK_SIZE,
        callback=callback,
    ):
        while len(all_frames) < max_frames:
            frame = audio_q.get()
            all_frames.append(frame)

            vad_flag = vad_fn(frame.tobytes())
            if energy_gate:
                rms = _frame_rms(frame)
                if not speech_started:
                    # Calibrate the room floor from pre-speech frames (median is
                    # robust to transients). Frozen once speech begins.
                    noise_samples.append(rms)
                    noise_floor = float(np.median(noise_samples[-30:]))
                is_speech = _is_speech_frame(vad_flag, rms, noise_floor)
            else:
                is_speech = vad_flag  # trust the neural VAD directly

            if is_speech:
                if not speech_started:
                    first_speech_idx = len(all_frames) - 1
                    speech_started = True
                consecutive_silence = 0
            elif speech_started:
                consecutive_silence += 1
                if consecutive_silence >= silence_frames:
                    break

    start = _speech_window_start(first_speech_idx, pad_frames)
    if start is None:
        return np.array([], dtype=DTYPE)
    return np.concatenate(all_frames[start:])


def _safe_complete(is_complete_fn: Callable[[Any], bool], transcript: Any) -> bool:
    """Run the completeness check, defaulting to 'complete' on any error — a buggy
    detector must never hold the mic open forever."""
    try:
        return bool(is_complete_fn(transcript))
    except Exception:  # noqa: BLE001
        return True


def record_with_vad_streaming(
    vad_fn: Callable[[bytes], bool],
    transcribe_fn: Callable[[np.ndarray], Any],
    *,
    sample_rate: int = SAMPLE_RATE,
    silence_threshold_s: float = 0.5,
    max_silence_threshold_s: float | None = None,
    is_complete_fn: Callable[[Any], bool] | None = None,
    max_duration_s: float = 30.0,
    pre_speech_pad_s: float = 0.3,
) -> tuple[np.ndarray, Any]:
    """Record until end-of-speech, transcribing during the silence-endpoint wait.

    The moment speech pauses, the captured audio is already the whole utterance, so
    transcribe_fn(audio) is fired in a background thread right then. By the time the
    silence threshold (~silence_threshold_s) confirms the turn is over, the
    transcript is usually ready — taking STT off the critical path.

    is_complete_fn enables semantic endpointing: at the base silence threshold the
    transcript-so-far is checked, and if it trails mid-sentence the turn is held open
    until max_silence_threshold_s of silence (then it gives up). Without it, the first
    silence_threshold_s pause ends the turn (plain endpointing).

    transcribe_fn returns an opaque STT result (this module stays STT-agnostic).
    Returns (audio_int16, result); result is None when the turn ends by the
    max-duration cutoff with no pause — the caller should then transcribe directly.
    """
    if hasattr(vad_fn, "reset"):
        vad_fn.reset()
    silence_frames = int(silence_threshold_s * sample_rate / BLOCK_SIZE)
    max_silence_frames = (
        int(max_silence_threshold_s * sample_rate / BLOCK_SIZE)
        if max_silence_threshold_s else silence_frames
    )
    max_frames = int(max_duration_s * sample_rate / BLOCK_SIZE)
    pad_frames = int(pre_speech_pad_s * sample_rate / BLOCK_SIZE)

    audio_q: queue.Queue[np.ndarray] = queue.Queue()

    def callback(indata: np.ndarray, frames: int, time_info: object, status: object) -> None:
        audio_q.put(indata[:, 0].copy())

    all_frames: list[np.ndarray] = []
    consecutive_silence = 0
    speech_started = False
    first_speech_idx: int | None = None
    executor = ThreadPoolExecutor(max_workers=1)
    pending: Future[Any] | None = None

    try:
        with sd.InputStream(
            device=_INPUT_DEVICE,
            samplerate=sample_rate,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=BLOCK_SIZE,
            callback=callback,
        ):
            while len(all_frames) < max_frames:
                frame = audio_q.get()
                all_frames.append(frame)
                if vad_fn(frame.tobytes()):
                    if not speech_started:
                        first_speech_idx = len(all_frames) - 1
                        speech_started = True
                    consecutive_silence = 0
                elif speech_started:
                    consecutive_silence += 1
                    if consecutive_silence == 1:
                        # Speech just paused — kick off transcription now so it
                        # overlaps the silence-confirmation wait. A later resume just
                        # supersedes this with a fresh submit at the next pause.
                        if pending is not None:
                            pending.cancel()
                        ws = _speech_window_start(first_speech_idx, pad_frames)
                        clip = np.concatenate(all_frames[ws:] if ws is not None else all_frames)
                        pending = executor.submit(transcribe_fn, clip)
                    if consecutive_silence >= silence_frames:
                        if is_complete_fn is None:
                            break  # plain endpointing — the first real pause ends it
                        # Semantic endpointing: end only if the thought looks finished;
                        # if it trails mid-sentence, keep listening to max_silence_frames.
                        transcript = (
                            pending.result() if (pending is not None and pending.done()) else None
                        )
                        complete = (
                            _safe_complete(is_complete_fn, transcript)
                            if transcript is not None else None
                        )
                        if turn_detector.endpoint_should_end(
                            complete, consecutive_silence, silence_frames, max_silence_frames
                        ):
                            break

        start = _speech_window_start(first_speech_idx, pad_frames)
        if start is None:
            return np.array([], dtype=DTYPE), None
        audio = np.concatenate(all_frames[start:])
        result: Any = None
        if pending is not None:
            try:
                result = pending.result()  # the last pause covers the full utterance
            except Exception:  # noqa: BLE001 — caller transcribes synchronously instead
                result = None
        return audio, result
    finally:
        executor.shutdown(wait=False)


def play_audio(audio: np.ndarray, sample_rate: int = 24000, blocking: bool = True) -> None:
    """Play float32 or int16 audio array through the default output device."""
    if audio.dtype == np.int16:
        audio = audio.astype(np.float32) / 32768.0
    sd.play(audio, samplerate=sample_rate, device=_OUTPUT_DEVICE, blocking=blocking)


def play_audio_stream(
    chunk_iter: Callable[[], np.ndarray | None],
    sample_rate: int = 24000,
) -> None:
    """Stream audio chunks to speaker as they arrive — for streaming TTS."""
    with sd.OutputStream(
        samplerate=sample_rate, channels=1, dtype=np.float32, device=_OUTPUT_DEVICE
    ) as stream:
        while True:
            chunk = chunk_iter()
            if chunk is None:
                break
            if chunk.dtype == np.int16:
                chunk = chunk.astype(np.float32) / 32768.0
            stream.write(chunk)


# Sub-block size for interruptible writes: ~85ms @ 24kHz. Small enough that the
# barge-in flag is checked often; large enough to keep write overhead negligible.
_PLAY_SUBBLOCK = 2048


def play_chunks_interruptible(
    next_chunk: Callable[[], np.ndarray | None],
    *,
    vad: Any,
    sample_rate: int = 24000,
    detector: BargeInDetector | None = None,
    input_sample_rate: int = SAMPLE_RATE,
) -> bool:
    """Play TTS chunks while listening for the user to talk over them (barge-in).

    ``next_chunk()`` returns the next float32/int16 audio block, or None when the
    reply is fully spoken. A side thread reads the mic at ``input_sample_rate``,
    runs Silero VAD on each frame and feeds ``detector``; the instant it fires,
    playback is aborted mid-word.

    ``vad`` is a SileroVAD-like object exposing ``.reset()`` and ``.prob(frame)``.
    Returns True if interrupted, False if playback finished normally. A mic glitch
    in the monitor is swallowed — it degrades to plain playback, never a crash.
    """
    detector = detector or BargeInDetector()
    interrupted = threading.Event()
    stop = threading.Event()

    def monitor() -> None:
        try:
            vad.reset()
            detector.reset()
            with sd.InputStream(
                device=_INPUT_DEVICE,
                samplerate=input_sample_rate,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=BLOCK_SIZE,
            ) as mic:
                while not stop.is_set():
                    frame, _ = mic.read(BLOCK_SIZE)
                    mono = frame[:, 0]
                    if detector.feed(vad.prob(mono.tobytes()), _frame_rms(mono)):
                        interrupted.set()
                        return
        except Exception:  # noqa: BLE001 — a mic hiccup must never crash playback
            _log.exception("barge-in monitor failed; finishing playback without it")

    mon = threading.Thread(target=monitor, name="barge-in-monitor", daemon=True)
    mon.start()
    try:
        with sd.OutputStream(
            samplerate=sample_rate, channels=CHANNELS, dtype=np.float32, device=_OUTPUT_DEVICE
        ) as out:
            while not interrupted.is_set():
                chunk = next_chunk()
                if chunk is None:
                    break
                if chunk.dtype == np.int16:
                    chunk = chunk.astype(np.float32) / 32768.0
                for i in range(0, len(chunk), _PLAY_SUBBLOCK):
                    if interrupted.is_set():
                        break
                    out.write(chunk[i : i + _PLAY_SUBBLOCK])
            if interrupted.is_set():
                out.abort()  # drop buffered audio → stop now, mid-word
    finally:
        stop.set()
        mon.join(timeout=1.0)
    return interrupted.is_set()
