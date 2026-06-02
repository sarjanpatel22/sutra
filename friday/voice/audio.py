"""Audio I/O helpers — mic recording and speaker playback via sounddevice."""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable

import numpy as np
import sounddevice as sd

import os

SAMPLE_RATE = 16000   # Hz — Whisper native
CHANNELS = 1
DTYPE = np.int16
BLOCK_SIZE = 480      # 30ms at 16kHz — webrtcvad frame size

# Set AUDIO_INPUT_DEVICE / AUDIO_OUTPUT_DEVICE in .env to override.
# "pulse" routes through PipeWire/PulseAudio and respects system defaults.
_INPUT_DEVICE: str | int | None = os.getenv("AUDIO_INPUT_DEVICE") or None
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


def record_with_vad(
    vad_fn: Callable[[bytes], bool],
    *,
    sample_rate: int = SAMPLE_RATE,
    silence_threshold_s: float = 0.35,
    max_duration_s: float = 30.0,
    pre_speech_pad_s: float = 0.3,
) -> np.ndarray:
    """Record until VAD detects end-of-speech (silence_threshold_s of silence).

    Returns int16 PCM array ready for Whisper.
    """
    frame_bytes = BLOCK_SIZE * 2  # int16 = 2 bytes per sample
    silence_frames = int(silence_threshold_s * sample_rate / BLOCK_SIZE)
    max_frames = int(max_duration_s * sample_rate / BLOCK_SIZE)
    pad_frames = int(pre_speech_pad_s * sample_rate / BLOCK_SIZE)

    audio_q: queue.Queue[np.ndarray] = queue.Queue()

    def callback(indata: np.ndarray, frames: int, time_info: object, status: object) -> None:
        audio_q.put(indata[:, 0].copy())

    all_frames: list[np.ndarray] = []
    speech_frames: list[np.ndarray] = []
    consecutive_silence = 0
    speech_started = False

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
            is_speech = vad_fn(frame.tobytes())

            if is_speech:
                speech_started = True
                consecutive_silence = 0
                # Include pre-speech padding
                start = max(0, len(all_frames) - len(speech_frames) - pad_frames)
                speech_frames = all_frames[start:]
            elif speech_started:
                speech_frames.append(frame)
                consecutive_silence += 1
                if consecutive_silence >= silence_frames:
                    break

    return np.concatenate(speech_frames) if speech_frames else np.array([], dtype=DTYPE)


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
