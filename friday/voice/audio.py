"""Audio I/O helpers — mic recording and speaker playback via sounddevice."""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Callable

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000   # Hz — Whisper native
CHANNELS = 1
DTYPE = np.int16
BLOCK_SIZE = 480      # 30ms at 16kHz — webrtcvad frame size

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


def record_with_vad(
    vad_fn: Callable[[bytes], bool],
    *,
    sample_rate: int = SAMPLE_RATE,
    silence_threshold_s: float = 0.35,
    max_duration_s: float = 30.0,
    pre_speech_pad_s: float = 0.3,
) -> np.ndarray:
    """Record until VAD detects end-of-speech (silence_threshold_s of silence).

    Captures from pad_frames before the first detected speech through end-of-speech.
    Returns int16 PCM array ready for Whisper.
    """
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
    # Ambient noise floor, estimated from the quiet lead-in before speech starts.
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

            rms = _frame_rms(frame)
            vad_flag = vad_fn(frame.tobytes())
            if not speech_started:
                # Calibrate the room noise floor from pre-speech frames (median is
                # robust to the odd transient). Frozen once speech begins.
                noise_samples.append(rms)
                noise_floor = float(np.median(noise_samples[-30:]))

            if _is_speech_frame(vad_flag, rms, noise_floor):
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


def play_audio(audio: np.ndarray, sample_rate: int = 24000, blocking: bool = True) -> None:
    """Play float32 or int16 audio array through the default output device."""
    if audio.dtype == np.int16:
        audio = audio.astype(np.float32) / 32768.0
    sd.play(audio, samplerate=sample_rate, device=_OUTPUT_DEVICE, blocking=blocking)


def play_audio_stream(
    chunk_iter: "Callable[[], np.ndarray | None]",
    sample_rate: int = 24000,
) -> None:
    """Stream audio chunks to speaker as they arrive — for streaming TTS."""
    with sd.OutputStream(samplerate=sample_rate, channels=1, dtype=np.float32, device=_OUTPUT_DEVICE) as stream:
        while True:
            chunk = chunk_iter()
            if chunk is None:
                break
            if chunk.dtype == np.int16:
                chunk = chunk.astype(np.float32) / 32768.0
            stream.write(chunk)
