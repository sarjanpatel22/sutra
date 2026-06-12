"""Non-interactive pre-flight for barge-in: validates the audio-device mechanism
on real hardware (full-duplex InputStream+OutputStream, abort mid-playback)
without needing a human at the mic. A short quiet tone will play twice.

  Test A: VAD never fires  → plays all chunks, returns False (no false interrupt).
  Test B: VAD always fires → aborts playback early, returns True.

If this passes, the live behavioral test only has to confirm that *real speech*
trips the detector and that it feels right.
"""
from __future__ import annotations

import time

import numpy as np

from friday.voice.audio import BargeInDetector, play_chunks_interruptible


class FakeVAD:
    """Stand-in for SileroVAD with a fixed speech probability."""

    def __init__(self, prob: float) -> None:
        self._p = prob

    def reset(self) -> None:
        pass

    def prob(self, frame: object) -> float:
        return self._p


def _tone_chunks(seconds: float = 0.9, sr: int = 24000, n: int = 3):
    """Yield n equal chunks of a gentle 330 Hz tone, then None."""
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    wave = (0.05 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)
    pieces = np.array_split(wave, n)
    idx = 0

    def nxt():
        nonlocal idx
        if idx >= len(pieces):
            return None
        c = pieces[idx]
        idx += 1
        return c

    return nxt


def run(name: str, *, vad_prob: float, detector: BargeInDetector, expect: bool) -> bool:
    t0 = time.monotonic()
    got = play_chunks_interruptible(
        _tone_chunks(),
        vad=FakeVAD(vad_prob),
        sample_rate=24000,
        detector=detector,
    )
    dt = (time.monotonic() - t0) * 1000
    ok = got is expect
    print(
        f"  {name}: interrupted={got} (expected {expect})  {dt:.0f}ms"
        f"  -> {'PASS' if ok else 'FAIL'}"
    )
    return ok


def main() -> int:
    print("barge-in mechanism pre-flight (real audio devices)")
    try:
        a = run(
            "A never-fire ",
            vad_prob=0.0,
            detector=BargeInDetector(),
            expect=False,
        )
        # rms_floor=0 so it fires on prob alone regardless of room loudness.
        b = run(
            "B always-fire",
            vad_prob=0.99,
            detector=BargeInDetector(rms_floor=0.0, grace_frames=2, trigger_frames=2),
            expect=True,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  AUDIO ERROR: {type(e).__name__}: {e}")
        print("  -> couldn't open audio from this shell; run the live test directly.")
        return 2
    print("RESULT:", "PASS — mechanism works on this hardware" if (a and b) else "FAIL — see above")
    return 0 if (a and b) else 1


if __name__ == "__main__":
    raise SystemExit(main())
