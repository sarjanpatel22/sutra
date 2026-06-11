"""Tests for looks_like_noise — the memory persistence quality gate.

Keeps STT garbage (far-field / echo transcriptions like "M-H-H-H-H") out of the
graph while letting real utterances and normal short commands through. It's a
deliberately conservative backstop; plausible-but-wrong single words are caught
upstream by STT confidence, not here.
"""

from friday.memory.store import (
    looks_like_narration_fact,
    looks_like_noise,
    looks_low_value,
)


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


# ── looks_low_value: ambient telemetry / dialogue narration, not facts ──────────
def test_activity_telemetry_is_low_value():
    for junk in (
        "Sarjan spent about 8m in Terminal (mostly on Resume previous coding session).",
        "Sarjan spent about 7m in firefox-esr (mostly on (388) Some Show — Mozilla Firefox).",
        "Sarjan spent about 1m in xfdesktop (mostly on Desktop).",
        "Sarjan used the Terminal for about 11 minutes.",
    ):
        assert looks_low_value(junk) is True, junk


def test_browsing_and_dialogue_narration_is_low_value():
    for junk in (
        "Sarjan was on a YouTube video about cats.",
        "The User is talking to Sutra.",
        "User mentions that Joyer is playing FIFA.",
        "Sutra responded to the user.",
        "Sutra is upgrading Sutra to Gemma 4.",
    ):
        assert looks_low_value(junk) is True, junk


def test_real_facts_are_not_low_value():
    for fact in (
        "Sarjan is a software engineer at Acme Corp, based in Toronto.",
        "Sutra's memory layer is built on Graphiti and FalkorDB, with Qdrant for vectors.",
        "Sutra's voice loop uses faster-whisper for STT and Kokoro for TTS.",
        "Sarjan edited store.py and test_memory.py in the friday-os project.",
        "Sarjan is from Sudbury, Ontario, Canada.",
    ):
        assert looks_low_value(fact) is False, fact


def test_verbatim_turns_are_never_low_value():
    # A turn can carry a user-stated fact; the extractor mines it. Never auto-flag.
    assert looks_low_value(
        "User: My cat is named Machai, and she is a grey British shorthair. "
        "Sutra: That's lovely, Sarjan."
    ) is False
    # even a chit-chat turn that *mentions* an app stays (it's verbatim, low risk)
    assert looks_low_value("User: open firefox for me. Sutra: Done.") is False


def test_low_value_empty_is_false():
    assert looks_low_value("") is False  # nothing to flag; not a deletable episode


# ── looks_like_narration_fact: edge-level dialogue narration, not knowledge ──────
def test_narration_facts_are_flagged():
    # Real edges purged from the graph (recall had no use for them, out-ranked facts).
    for junk in (
        "Sutra responded to the user.",
        "Sutra offers to assist User with their order.",
        "Sutra addresses Sarjan.",
        "Sutra expressed pleasure at speaking with the user.",
        "Sutra told User that 17 times 23 is 391.",
        "Sutra intends to scan for Hedra as per the user's request.",
        "User asked Sutra what 17 times 23 is.",
        "The user said hello to Sutra.",
        "User commented on the Reitutra system.",
        "The User is talking to Sutra.",
        "Let's take a moment, Sarjan.",
    ):
        assert looks_like_narration_fact(junk) is True, junk


def test_real_facts_are_not_narration():
    # Capability / profile / spec facts share the "Sutra"/"Sarjan" subject but are
    # NOT communication verbs — and the atomic descriptor we seed must survive.
    for fact in (
        "Sutra is a voice-native ambient AI assistant.",
        "Sutra's memory layer is built on Graphiti and FalkorDB, with Qdrant for vectors.",
        "Sutra uses qwen2.5:14b for memory extraction.",
        "Sutra runs Ollama locally for the fast voice path.",
        "Sarjan is a software engineer at Acme Corp.",
        "Sarjan's GPU has 16 GB of VRAM (16384 MB).",
        "Sarjan is from Sudbury, Ontario, Canada.",
    ):
        assert looks_like_narration_fact(fact) is False, fact


def test_narration_empty_is_false():
    assert looks_like_narration_fact("") is False
