"""Tests for the VAD capture windowing logic in friday.voice.audio.

Covers the pre-speech-padding math that was previously buggy (it walked the
start index backward every speech frame, dragging in all leading silence).
The live mic path still needs a manual `sutra voice --mode vad` check.
"""

from friday.voice.audio import _speech_window_start


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
