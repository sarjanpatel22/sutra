"""Tests for the barge-in decision logic in friday.voice.audio.BargeInDetector.

Pure-logic coverage of the guards that let Sutra stop speaking when the user
talks over her without an echo canceller: grace period, speech-prob threshold,
RMS energy floor, and the sustained-frame requirement. The live mic→speaker
path still needs a manual `VOICE_BARGE_IN=1 sutra voice --mode vad` check
(ideally on a headset, where there's no echo to fight).
"""

from friday.voice.audio import BargeInDetector


def _feed(det: BargeInDetector, n: int, *, prob: float = 0.9, rms: float = 2000.0) -> bool:
    """Feed n identical frames; return whether the detector fired by the last one."""
    fired = False
    for _ in range(n):
        fired = det.feed(prob, rms)
    return fired


def test_grace_suppresses_early_speech():
    # Loud, confident speech during the grace window must NOT fire — that window
    # exists precisely so the first TTS chunk can't self-trigger via echo.
    det = BargeInDetector(grace_frames=8, trigger_frames=3)
    assert _feed(det, 8) is False


def test_fires_after_sustained_speech_past_grace():
    det = BargeInDetector(grace_frames=2, trigger_frames=3)
    assert _feed(det, 2) is False          # grace
    assert det.feed(0.9, 2000.0) is False  # streak 1
    assert det.feed(0.9, 2000.0) is False  # streak 2
    assert det.feed(0.9, 2000.0) is True   # streak 3 → fire


def test_streak_must_be_consecutive():
    det = BargeInDetector(grace_frames=0, trigger_frames=3)
    det.feed(0.9, 2000.0)                   # streak 1
    det.feed(0.9, 2000.0)                   # streak 2
    assert det.feed(0.2, 2000.0) is False  # quiet frame resets the streak
    assert _feed(det, 2) is False          # only 2 of 3 again
    assert det.feed(0.9, 2000.0) is True   # 3rd consecutive → fire


def test_low_prob_never_fires():
    # Below the speech-prob threshold (echo bleed gives weak prob) → never speech.
    det = BargeInDetector(grace_frames=0, trigger_frames=2, threshold=0.7)
    assert _feed(det, 30, prob=0.5, rms=9000.0) is False


def test_quiet_echo_below_floor_never_fires():
    # Confident prob but attenuated open-speaker echo below the RMS floor → ignored.
    det = BargeInDetector(grace_frames=0, trigger_frames=2, rms_floor=500.0)
    assert _feed(det, 30, prob=0.95, rms=300.0) is False


def test_reset_rearms_grace():
    det = BargeInDetector(grace_frames=2, trigger_frames=1)
    assert _feed(det, 3) is True   # past grace, fires
    det.reset()
    assert det.feed(0.9, 2000.0) is False  # grace re-applies after reset


def test_from_env_defaults(monkeypatch):
    for k in ("VOICE_BARGE_THRESHOLD", "VOICE_BARGE_RMS_FLOOR",
              "VOICE_BARGE_GRACE_S", "VOICE_BARGE_SUSTAIN_S"):
        monkeypatch.delenv(k, raising=False)
    det = BargeInDetector.from_env()
    assert det.threshold == 0.7
    assert det.rms_floor == 500.0
    assert det.grace_frames == 8    # 0.25s × 31.25 fps
    assert det.trigger_frames == 6  # 0.20s × 31.25 fps


def test_from_env_parses_seconds(monkeypatch):
    monkeypatch.setenv("VOICE_BARGE_THRESHOLD", "0.8")
    monkeypatch.setenv("VOICE_BARGE_RMS_FLOOR", "750")
    monkeypatch.setenv("VOICE_BARGE_GRACE_S", "0.32")   # ×31.25 → 10
    monkeypatch.setenv("VOICE_BARGE_SUSTAIN_S", "0.16")  # ×31.25 → 5
    det = BargeInDetector.from_env()
    assert det.threshold == 0.8
    assert det.rms_floor == 750.0
    assert det.grace_frames == 10
    assert det.trigger_frames == 5
