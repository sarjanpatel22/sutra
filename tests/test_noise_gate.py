"""Tests for looks_like_noise — the memory persistence quality gate.

Keeps STT garbage (far-field / echo transcriptions like "M-H-H-H-H") out of the
graph while letting real utterances and normal short commands through. It's a
deliberately conservative backstop; plausible-but-wrong single words are caught
upstream by STT confidence, not here.
"""

from friday.memory.store import looks_like_noise


def test_empty_and_whitespace_are_noise():
    assert looks_like_noise("") is True
    assert looks_like_noise("   ") is True


def test_no_real_letters_is_noise():
    assert looks_like_noise("...!?") is True
    assert looks_like_noise("123 456") is True


def test_single_letter_spellout_is_noise():
    assert looks_like_noise("M-H-H-H-H-H-H") is True
    assert looks_like_noise("M H H H") is True


def test_repeated_char_run_is_noise():
    assert looks_like_noise("Hhhhhh") is True
    assert looks_like_noise("aaaa") is True
    assert looks_like_noise("soooooo") is True


def test_normal_short_commands_pass():
    for ok in ("yes", "no", "stop it", "hi there", "what's the weather"):
        assert looks_like_noise(ok) is False, ok


def test_normal_sentences_pass():
    assert looks_like_noise("Count slowly from one to twenty.") is False
    # "I" and "a" are single letters but must not trip the gate in real prose.
    assert looks_like_noise("I'll be there in a bit, I think.") is False


def test_plausible_gibberish_word_passes_heuristic():
    # Documents the boundary: a single nonsense word isn't caught here by design
    # (STT avg_logprob handles those). The gate only rejects obvious junk shapes.
    assert looks_like_noise("reitutra") is False
