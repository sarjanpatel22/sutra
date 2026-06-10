"""Lightweight semantic end-of-turn detection.

Silence alone ends a turn too early — a thinking pause mid-sentence gets cut off.
This adds a cheap linguistic check: if the utterance so far trails on a word or
phrase that clearly expects more ("and", "the", "i'm", "going to", "um"...), the
speaker isn't done, so the recorder waits longer before ending the turn. $0/local,
no model — just high-precision cues, biased to only EXTEND on strong evidence so a
normal complete sentence never feels laggy.
"""

from __future__ import annotations

import re

# Last word strongly implies continuation (rarely ends a natural utterance). Kept
# conservative — ambiguous enders like "it"/"you"/"like" are excluded so common
# endings ("thank you", "do it") aren't mistaken for mid-thought pauses.
_TRAILING_WORDS = frozenset({
    # conjunctions
    "and", "but", "or", "nor", "so", "because", "cause", "cuz", "since",
    "although", "though", "whereas", "plus", "yet",
    # articles / possessives (expect a noun next)
    "the", "a", "an", "my", "your", "our", "their",
    # intent contractions (expect a verb/object next)
    "im", "ima", "gonna", "wanna", "gotta",
    "i'm", "i'll", "i've", "i'd", "we'll", "we're", "they're", "that's", "there's",
    # fillers
    "um", "uh", "uhh", "uhm", "er", "erm", "hmm", "mmm",
    # prepositions that essentially never end an utterance
    "into", "onto", "upon", "of",
})

# Utterance ends on one of these phrases → still going.
_TRAILING_PHRASES = (
    "going to", "want to", "trying to", "need to", "have to", "has to", "had to",
    "about to", "used to", "supposed to", "in order to", "so that",
    "kind of", "sort of", "a lot of", "because of", "as well as", "such as",
    "i mean", "you know", "i guess", "i think", "let me",
)

_WORD_RE = re.compile(r"[a-z0-9']+")


def is_complete_utterance(text: str) -> bool:
    """True if `text` looks like a finished thought (so the turn can end)."""
    core = (text or "").strip().lower().rstrip(".!?,;: ")
    if not core:
        return True  # nothing transcribed → don't hold the turn open
    if any(core.endswith(phrase) for phrase in _TRAILING_PHRASES):
        return False
    words = _WORD_RE.findall(core)
    if words and words[-1] in _TRAILING_WORDS:
        return False
    return True


def endpoint_should_end(
    complete: bool | None, current_silence: int, base_frames: int, max_frames: int
) -> bool:
    """End the turn now?

    complete: detector verdict, or None when the transcript isn't ready yet.
    Ends at `base_frames` of silence if the thought looks complete; otherwise keeps
    listening until `max_frames` of silence, then gives up (they trailed off).
    """
    if current_silence < base_frames:
        return False
    if current_silence >= max_frames:
        return True
    return bool(complete)  # None/False → keep waiting; True → end
