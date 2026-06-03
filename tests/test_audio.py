"""Tests for the VAD capture windowing logic in friday.voice.audio.

Covers the pre-speech-padding math that was previously buggy (it walked the
start index backward every speech frame, dragging in all leading silence).
The live mic path still needs a manual `sutra voice --mode vad` check.
"""

import numpy as np

from friday.voice.audio import _frame_rms, _is_speech_frame, _speech_window_start


def test_no_speech_returns_none():
    assert _speech_window_start(None, pad_frames=10) is None


def test_pad_applied_before_first_speech():
    # speech first seen at frame 20, 10 frames of pad → start at 10
    assert _speech_window_start(20, pad_frames=10) == 10


def test_pad_clamped_to_zero():
    # speech starts before a full pad is available → clamp at 0, no negative index
    assert _speech_window_start(5, pad_frames=10) == 0


def test_zero_pad():
    assert _speech_window_start(20, pad_frames=0) == 20


def test_speech_at_start():
    assert _speech_window_start(0, pad_frames=10) == 0


# --- energy gate (the fix for VAD keeping the mic open on room noise) ---

def test_frame_rms_empty_is_zero():
    assert _frame_rms(np.array([], dtype=np.int16)) == 0.0


def test_frame_rms_known_value():
    # constant amplitude 1000 → RMS 1000
    assert _frame_rms(np.full(480, 1000, dtype=np.int16)) == 1000.0


def test_loud_speech_frame_passes():
    # webrtcvad says speech AND well above the room floor → speech
    assert _is_speech_frame(True, rms=3000.0, noise_floor=200.0) is True


def test_ambient_noise_does_not_count_as_speech():
    # THE BUG: webrtcvad flags steady room noise as speech. With the energy gate,
    # a frame near the noise floor must NOT count as speech, so silence registers.
    assert _is_speech_frame(True, rms=210.0, noise_floor=200.0) is False


def test_vad_negative_never_speech():
    assert _is_speech_frame(False, rms=9000.0, noise_floor=100.0) is False


def test_absolute_minimum_floor():
    # In a dead-silent room (noise_floor≈0) a quiet frame still needs real signal.
    assert _is_speech_frame(True, rms=100.0, noise_floor=0.0) is False
    assert _is_speech_frame(True, rms=400.0, noise_floor=0.0) is True
