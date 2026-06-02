"""Voice Activity Detection using webrtcvad."""

from __future__ import annotations

import webrtcvad


class VAD:
    """Thin wrapper around webrtcvad.

    aggressiveness: 0 (least aggressive) to 3 (most aggressive).
    Mode 2 is a good default — catches speech clearly without cutting off.
    """

    def __init__(self, aggressiveness: int = 2, sample_rate: int = 16000) -> None:
        if sample_rate not in (8000, 16000, 32000, 48000):
            raise ValueError(f"webrtcvad only supports 8/16/32/48 kHz, got {sample_rate}")
        self._vad = webrtcvad.Vad(aggressiveness)
        self._sample_rate = sample_rate

    def is_speech(self, pcm_bytes: bytes) -> bool:
        """Return True if the frame contains speech.

        Frame must be exactly 10, 20, or 30ms of 16-bit PCM.
        At 16kHz: 10ms=320B, 20ms=640B, 30ms=960B.
        """
        try:
            return self._vad.is_speech(pcm_bytes, self._sample_rate)
        except Exception:  # noqa: BLE001
            return False

    def __call__(self, pcm_bytes: bytes) -> bool:
        return self.is_speech(pcm_bytes)
