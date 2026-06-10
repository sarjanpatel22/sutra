"""Tests for Sutra's persona/mood selection."""

from friday.core import persona
from friday.core.persona import DEFAULT_MOOD, resolve_mood, voice_system_prompt


def test_explicit_command_switches_and_sticks():
    turn, new, why = resolve_mood("ok lock in and help me", DEFAULT_MOOD)
    assert (turn, new, why) == ("locked_in", "locked_in", "command")


def test_each_mood_has_a_command():
    assert resolve_mood("be sarcastic", "chill")[0] == "sarcastic"
    assert resolve_mood("hey be nice to me", "chill")[0] == "warm"
    assert resolve_mood("lets go!!", "chill")[0] == "fired_up"  # apostrophe-free match
    assert resolve_mood("alright chill out", "locked_in")[0] == "chill"


def test_cue_is_per_turn_not_persistent():
    turn, new, why = resolve_mood("how do i fix this build error", "chill")
    assert turn == "locked_in"      # situational nudge
    assert new == "chill"           # persistent baseline unchanged
    assert why == "situation"


def test_task_intent_beats_chitchat_cue():
    # both "haha" (chill cue) and "fix"/"build" (locked_in cue) present → task wins
    assert resolve_mood("haha can you fix the build", "chill")[0] == "locked_in"


def test_default_keeps_persistent_mood():
    out = resolve_mood("what is two plus two", "sarcastic")
    assert out == ("sarcastic", "sarcastic", "default")


def test_prompt_carries_persona_mood_and_rules():
    p = voice_system_prompt("sarcastic")
    assert "Sutra" in p and "sarcastic" in p.lower()
    assert "no markdown" in p.lower()                       # spoken-voice rules present
    assert voice_system_prompt("bogus") == voice_system_prompt(DEFAULT_MOOD)  # unknown → default


def test_active_mood_for_commits_and_is_sticky():
    persona.set_mood("chill")
    assert persona.active_mood_for("lock in please")[0] == "locked_in"
    assert persona.get_mood() == "locked_in"               # explicit switch persisted
    assert persona.active_mood_for("what time is it")[0] == "locked_in"  # sticky
    persona.set_mood(DEFAULT_MOOD)                          # reset for other tests
