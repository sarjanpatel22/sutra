"""Wake-gate tests — pure threshold/streak/cooldown logic, no onnx model and no mic."""

from __future__ import annotations

import pytest

from friday.voice.wakeword import WakeGate, make_detector, wake_enabled


@pytest.mark.parametrize(
    "val,want",
    [("1", True), ("true", True), ("YES", True), ("0", False), ("false", False), ("", False)],
)
def test_wake_enabled_parsing(monkeypatch, val, want):
    monkeypatch.setenv("WAKEWORD_ENABLED", val)
    assert wake_enabled() is want


def test_wake_disabled_when_unset(monkeypatch):
    monkeypatch.delenv("WAKEWORD_ENABLED", raising=False)
    assert wake_enabled() is False


def test_fires_when_score_crosses_threshold():
    g = WakeGate(threshold=0.5, cooldown_s=2.0, trigger_frames=1)
    assert g.feed(0.4, now=100.0) is False
    assert g.feed(0.9, now=100.1) is True


def test_requires_consecutive_trigger_frames():
    g = WakeGate(threshold=0.5, cooldown_s=0.0, trigger_frames=3)
    assert g.feed(0.9, now=1.0) is False
    assert g.feed(0.9, now=1.1) is False
    assert g.feed(0.9, now=1.2) is True


def test_streak_resets_on_a_low_score():
    g = WakeGate(threshold=0.5, cooldown_s=0.0, trigger_frames=2)
    assert g.feed(0.9, now=1.0) is False
    assert g.feed(0.1, now=1.1) is False  # breaks the streak
    assert g.feed(0.9, now=1.2) is False  # rebuilding
    assert g.feed(0.9, now=1.3) is True


def test_cooldown_blocks_immediate_refire():
    g = WakeGate(threshold=0.5, cooldown_s=2.0, trigger_frames=1)
    assert g.feed(0.9, now=10.0) is True
    assert g.feed(0.9, now=10.5) is False   # inside the cooldown window
    assert g.feed(0.9, now=12.1) is True    # cooldown elapsed → fires again


def test_from_env_reads_knobs(monkeypatch):
    monkeypatch.setenv("WAKEWORD_THRESHOLD", "0.7")
    monkeypatch.setenv("WAKEWORD_COOLDOWN_S", "5")
    monkeypatch.setenv("WAKEWORD_TRIGGER_FRAMES", "4")
    g = WakeGate.from_env()
    assert (g.threshold, g.cooldown_s, g.trigger_frames) == (0.7, 5.0, 4)


def test_make_detector_combines_score_and_gate():
    # Inject a fake score stream — no model, no mic. Frame content is irrelevant.
    scores = iter([0.1, 0.2, 0.95])
    gate = WakeGate(threshold=0.5, cooldown_s=0.0, trigger_frames=1)
    feed = make_detector(score_fn=lambda _frame: next(scores), gate=gate)
    assert feed(None) is False
    assert feed(None) is False
    assert feed(None) is True
