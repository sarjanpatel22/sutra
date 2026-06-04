"""Text-to-speech via Kokoro ONNX — streaming-capable, GPU-accelerated."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_MODEL_DIR = Path.home() / ".local" / "share" / "friday" / "models" / "kokoro"
_ONNX_MODEL = _MODEL_DIR / "kokoro-v1.0.onnx"
_VOICES_FILE = _MODEL_DIR / "voices-v1.0.bin"

# Sentence boundary pattern — split here for streaming TTS chunks
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass
class TTSChunk:
    audio: np.ndarray   # float32, 24kHz mono
    sample_rate: int
    text: str
    latency_ms: float   # time from text-in to first audio out


class TTS:
    """Kokoro ONNX TTS wrapper.

    Supports streaming: feed sentences as they arrive, get audio chunks back.
    Sample rate: 24 kHz mono float32.
    """

    def __init__(self, voice: str | None = None, speed: float | None = None) -> None:
        import os
        from kokoro_onnx import Kokoro  # type: ignore[import-untyped]

        # Read at construction (after load_dotenv) — KOKORO_VOICE was previously
        # unwired: TTS() always used af_heart and ignored the .env setting.
        voice = voice or os.getenv("KOKORO_VOICE", "af_heart")
        speed = speed if speed is not None else float(os.getenv("KOKORO_SPEED", "1.0"))

        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
        model_path = os.getenv("KOKORO_MODEL_PATH", str(_ONNX_MODEL))
        voices_path = os.getenv("KOKORO_VOICES_PATH", str(_VOICES_FILE))
        # Default to GPU — CUDAExecutionProvider confirmed working
        if "ONNX_PROVIDER" not in os.environ:
            os.environ["ONNX_PROVIDER"] = "CUDAExecutionProvider"
        # Suppress verbose onnxruntime session warnings (memcpy, node assignment)
        import onnxruntime as rt
        rt.set_default_logger_severity(3)  # ERROR only
        self._kokoro = Kokoro(model_path, voices_path)
        self._voice = voice
        self._speed = speed
        self.sample_rate = 24000

    def synthesize(self, text: str) -> TTSChunk:
        """Synthesize full text. Returns a single audio chunk."""
        text = text.strip()
        if not text:
            return TTSChunk(
                audio=np.array([], dtype=np.float32),
                sample_rate=self.sample_rate,
                text=text,
                latency_ms=0.0,
            )
        t0 = time.monotonic()
        samples, sr = self._kokoro.create(text, voice=self._voice, speed=self._speed, lang="en-us")
        latency_ms = (time.monotonic() - t0) * 1000
        audio = np.array(samples, dtype=np.float32)
        return TTSChunk(audio=audio, sample_rate=sr, text=text, latency_ms=latency_ms)

    def stream(self, text_iter: Iterator[str]) -> Iterator[TTSChunk]:
        """Stream TTS: buffer tokens into sentences, yield audio chunks.

        Feed this an iterator of LLM text tokens. Each time a sentence
        boundary is detected, synthesize and yield the audio immediately —
        so playback starts before the LLM finishes generating.
        """
        buf = ""
        for token in text_iter:
            buf += token
            # Check for sentence boundary
            parts = _SENTENCE_RE.split(buf, maxsplit=1)
            while len(parts) > 1:
                sentence = parts[0].strip()
                if sentence:
                    yield self.synthesize(sentence)
                buf = parts[1]
                parts = _SENTENCE_RE.split(buf, maxsplit=1)

        # Flush remaining buffer
        if buf.strip():
            yield self.synthesize(buf.strip())


_instance: TTS | None = None


def get_tts() -> TTS:
    global _instance
    if _instance is None:
        _instance = TTS()
    return _instance
