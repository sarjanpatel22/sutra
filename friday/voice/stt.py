"""Speech-to-text via faster-whisper (CTranslate2, GPU-accelerated)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel

_MODEL_DIR = Path.home() / ".local" / "share" / "friday" / "models"
# small.en transcribes a 3s clip in ~75ms vs large-v3's ~440ms — 6x faster with
# identical output on clear speech — the dominant cut to voice "dead air". Override
# with WHISPER_MODEL (e.g. large-v3 for noisy/multilingual). This env was previously
# unwired: STT() ignored it and always loaded large-v3.
_DEFAULT_MODEL = os.getenv("WHISPER_MODEL", "small.en")
# WHISPER_DEVICE was documented but unwired (STT ignored it). Now honoured.
_DEFAULT_DEVICE = os.getenv("WHISPER_DEVICE", "auto")
# Hot-path tuning (all env-overridable): fix the language so Whisper skips its
# per-call language-detection pass, and use greedy decoding (beam 1) — ~30% faster
# with identical text on clear speech. vad_filter defaults OFF because Silero VAD
# already trimmed the audio upstream; re-running Whisper's internal VAD is wasted work.
_DEFAULT_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "en") or None
_DEFAULT_BEAM = int(os.getenv("WHISPER_BEAM", "1"))


@dataclass
class TranscribeResult:
    text: str
    language: str
    latency_ms: float
    segments: list[dict[str, object]]
    # Whisper confidence, duration-weighted across segments. avg_logprob ~ -0.1..-0.5
    # on clean speech, more negative (< ~-1.0) when it's hallucinating quiet/far-field
    # audio; no_speech_prob is high (> ~0.6) when there's likely no real speech. Used
    # to keep low-SNR garbage out of long-term memory.
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0


class STT:
    """Wrapper around faster-whisper for GPU-accelerated transcription.

    Loads large-v3 INT8 on CUDA by default (~2 GB VRAM).
    Falls back to CPU if CUDA unavailable.
    """

    def __init__(
        self,
        model_size: str = _DEFAULT_MODEL,
        device: str = _DEFAULT_DEVICE,
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
        language: str | None = _DEFAULT_LANGUAGE,
        beam_size: int = _DEFAULT_BEAM,
        vad_filter: bool = False,
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
        # Duration-weighted confidence so a long clear segment isn't dragged down by
        # a short uncertain one (and vice versa). Empty audio -> neutral 0.0.
        durations = [max(s.end - s.start, 1e-3) for s in segments]
        total = sum(durations) or 1.0
        avg_logprob = sum(s.avg_logprob * d for s, d in zip(segments, durations)) / total
        no_speech_prob = sum(s.no_speech_prob * d for s, d in zip(segments, durations)) / total
        return TranscribeResult(
            text=text,
            language=info.language,
            latency_ms=latency_ms,
            segments=seg_dicts,
            avg_logprob=avg_logprob if segments else 0.0,
            no_speech_prob=no_speech_prob if segments else 0.0,
        )


_instance: STT | None = None


def get_stt() -> STT:
    """Return singleton STT instance (lazy-loaded on first call)."""
    global _instance
    if _instance is None:
        _instance = STT()
    return _instance
