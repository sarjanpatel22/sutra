"""Tests for semantic end-of-turn detection."""

from friday.voice.turn_detector import endpoint_should_end
from friday.voice.turn_detector import is_complete_utterance as ic


def test_finished_sentences_are_complete():
    for s in ["what time is it", "the meeting is at three", "yes please",
              "thank you", "do it", "that works for me", "stop"]:
        assert ic(s), s


def test_trailing_connectives_are_incomplete():
    for s in ["so i was thinking and", "let me check the", "i need a",
              "because", "i'm gonna", "we ran into", "the cause of"]:
        assert not ic(s), s


def test_trailing_phrases_are_incomplete():
    for s in ["i want to", "i'm going to", "it's kind of", "you know",
              "i have to", "the reason is sort of"]:
        assert not ic(s), s


def test_fillers_are_incomplete():
    assert not ic("um")
    assert not ic("so, uh")
    assert not ic("well i think")  # ends on the phrase "i think"


def test_empty_or_punctuation_only_is_complete():
    assert ic("") and ic("   ") and ic("...")


def test_trailing_punctuation_is_stripped_before_check():
    assert not ic("let me check the,")   # comma shouldn't hide the trailing "the"
    assert ic("we are done.")


def test_endpoint_waits_then_gives_up():
    # below the base silence → never end yet
    assert endpoint_should_end(True, current_silence=2, base_frames=5, max_frames=15) is False
    # at base + complete thought → end
    assert endpoint_should_end(True, current_silence=5, base_frames=5, max_frames=15) is True
    # at base but unfinished → keep waiting
    assert endpoint_should_end(False, current_silence=5, base_frames=5, max_frames=15) is False
    # transcript not ready yet → keep waiting
    assert endpoint_should_end(None, current_silence=6, base_frames=5, max_frames=15) is False
    # patience exhausted → end regardless
    assert endpoint_should_end(False, current_silence=15, base_frames=5, max_frames=15) is True
