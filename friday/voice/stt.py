"""Speech-to-text via faster-whisper (CTranslate2, GPU-accelerated)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel

_MODEL_DIR = Path.home() / ".local" / "share" / "friday" / "models"
_DEFAULT_MODEL = "large-v3"


@dataclass
class TranscribeResult:
    text: str
    language: str
    latency_ms: float
    segments: list[dict[str, object]]


class STT:
    """Wrapper around faster-whisper for GPU-accelerated transcription.

    Loads large-v3 INT8 on CUDA by default (~2 GB VRAM).
    Falls back to CPU if CUDA unavailable.
    """

    def __init__(
        self,
        model_size: str = _DEFAULT_MODEL,
        device: str = "auto",
        compute_type: str = "int8",
    ) -> None:
        if device == "auto":
            try:
                import ctranslate2
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:  # noqa: BLE001
                device = "cpu"

        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
        self._model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            download_root=str(_MODEL_DIR),
        )
        self._device = device

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str | None = None,
        beam_size: int = 5,
        vad_filter: bool = True,
    ) -> TranscribeResult:
        """Transcribe int16 or float32 PCM audio."""
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0

        t0 = time.monotonic()
        segments_gen, info = self._model.transcribe(
            audio,
            language=language,
            beam_size=beam_size,
            vad_filter=vad_filter,
            vad_parameters={"min_silence_duration_ms": 300},
        )
        segments = list(segments_gen)
        latency_ms = (time.monotonic() - t0) * 1000

        text = " ".join(s.text.strip() for s in segments).strip()
        seg_dicts = [
            {"start": s.start, "end": s.end, "text": s.text.strip()}
            for s in segments
        ]
        return TranscribeResult(
            text=text,
            language=info.language,
            latency_ms=latency_ms,
            segments=seg_dicts,
        )


_instance: STT | None = None


def get_stt() -> STT:
    """Return singleton STT instance (lazy-loaded on first call)."""
    global _instance
    if _instance is None:
        _instance = STT()
    return _instance
