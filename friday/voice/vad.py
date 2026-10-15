"""Voice Activity Detection.

Two implementations:
  - SileroVAD  — neural (Silero v5 ONNX), noise-robust, the default. What modern
    voice stacks (LiveKit, Pipecat) use. Runs on CPU (model is ~1MB, ~1ms/frame)
    so it never competes with STT/LLM/TTS for VRAM.
  - VAD        — legacy webrtcvad wrapper. Frame-based, fooled by background noise
    (it fragmented real speech in testing). Kept for fallback/compat.

Both expose is_speech(frame)->bool and __call__(frame)->bool. SileroVAD is stateful
(an LSTM) so call reset() at the start of each utterance.
"""

from __future__ import annotations

import logging
import urllib.request
from pathlib import Path

import numpy as np

_log = logging.getLogger("sutra.voice.vad")

_MODEL_DIR = Path.home() / ".local" / "share" / "friday" / "models"
_SILERO_MODEL = _MODEL_DIR / "silero_vad.onnx"
# Canonical Silero v5 ONNX model (~1MB). Fetched once if missing.
_SILERO_URL = (
    "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
)
# Silero v5 @ 16kHz consumes 512-sample chunks prepended with 64 samples of
# carried context (576 total). Feeding bare 512 returns ~0 for everything.
_CHUNK = 512
_CONTEXT = 64


class SileroVAD:
    """Neural voice-activity detector (Silero v5 ONNX), CPU-run, stateful.

    threshold: speech probability cutoff (0–1). 0.5 is Silero's default.
    Call reset() between utterances to clear the LSTM state + context.
    """

    def __init__(self, threshold: float = 0.5, sample_rate: int = 16000) -> None:
        import onnxruntime as ort

        if not _SILERO_MODEL.exists():
            self._download()
        opts = ort.SessionOptions()
        opts.log_severity_level = 3  # ERROR only
        # CPU on purpose — the model is tiny; keeps the 16GB GPU free for STT/LLM/TTS.
        self._sess = ort.InferenceSession(
            str(_SILERO_MODEL), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._sr = np.array(sample_rate, dtype=np.int64)
        self._threshold = threshold
        self.reset()

    @staticmethod
    def _download() -> None:
        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
        _log.info("fetching Silero VAD model → %s", _SILERO_MODEL)
        urllib.request.urlretrieve(_SILERO_URL, _SILERO_MODEL)  # noqa: S310

    def reset(self) -> None:
        """Clear LSTM state + context — call at the start of each new utterance."""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._ctx = np.zeros((1, _CONTEXT), dtype=np.float32)

    def prob(self, frame: bytes | np.ndarray) -> float:
        """Speech probability (0–1) for one 512-sample frame."""
        if isinstance(frame, (bytes, bytearray)):
            frame = np.frombuffer(frame, dtype=np.int16)
        x = frame.astype(np.float32)
        if frame.dtype == np.int16:
            x = x / 32768.0
        if x.shape[0] < _CHUNK:
            x = np.pad(x, (0, _CHUNK - x.shape[0]))
        x = x[:_CHUNK].reshape(1, -1)
        inp = np.concatenate([self._ctx, x], axis=1)  # 64 context + 512 chunk
        try:
            out, self._state = self._sess.run(
                None, {"input": inp, "state": self._state, "sr": self._sr}
            )
        except Exception:  # noqa: BLE001 — never let VAD crash the loop
            return 0.0
        self._ctx = x[:, -_CONTEXT:]
        return float(out[0][0])

    def is_speech(self, frame: bytes | np.ndarray) -> bool:
        return self.prob(frame) >= self._threshold

    def __call__(self, frame: bytes | np.ndarray) -> bool:
        return self.is_speech(frame)


class VAD:
    """Legacy webrtcvad wrapper (frame-based, noise-sensitive). Prefer SileroVAD.

    aggressiveness: 0 (least) to 3 (most). Kept for fallback/compat.
    """

    def __init__(self, aggressiveness: int = 2, sample_rate: int = 16000) -> None:
        import webrtcvad

        if sample_rate not in (8000, 16000, 32000, 48000):
            raise ValueError(f"webrtcvad only supports 8/16/32/48 kHz, got {sample_rate}")
        self._vad = webrtcvad.Vad(aggressiveness)
        self._sample_rate = sample_rate

    def is_speech(self, pcm_bytes: bytes) -> bool:
        """Frame must be exactly 10/20/30ms of 16-bit PCM (160/320/480 samples @16k)."""
        try:
            return bool(self._vad.is_speech(pcm_bytes, self._sample_rate))
        except Exception:  # noqa: BLE001
            return False

    def __call__(self, pcm_bytes: bytes) -> bool:
        return self.is_speech(pcm_bytes)
