"""Sutra's personality: one base character + situational moods.

Voice replies were a flat butler because VOICE_SYSTEM_PROMPT carried no character.
This gives Sutra a consistent base persona and a set of moods that layer tone on
top — chosen from explicit commands ("lock in", "be sarcastic") or soft situational
cues, and sticky across turns until changed. $0/local: pure string composition +
keyword heuristics, no extra model call, no latency.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field

_USER_NAME = os.getenv("USER_NAME", "Sarjan")

# Who Sutra is regardless of mood. Deliberately not a butler.
BASE_PERSONA = (
    f"You are Sutra, {_USER_NAME}'s voice-native AI — sharp, fast, and real. Not a corporate "
    f"assistant, not a butler. You have a personality: quick wit, a little sarcasm, genuine warmth "
    f"underneath. You banter. You never grovel or pad with filler, never say things like "
    f'"How can I help you today?". '
    f'Recalled facts about {_USER_NAME} describe the person you\'re talking with — treat '
    f'"{_USER_NAME}" and "you/your" as the same person.'
)

_VOICE_RULES = (
    "Respond in spoken English only — natural sentences, no markdown, no lists, no code. "
    "Keep it short: usually under 15 words, more only if it truly needs it. "
    "Answer from your own knowledge; don't call tools or functions."
)
_VOICE_TOOL_RULES = (
    "You have real tools that actually DO things — open or show a website, search the web, "
    "navigate, run a task. When the user asks you to DO, OPEN, SHOW, START, or FIND something, "
    "you MUST call the matching tool to really do it. NEVER claim you did it ('right this way', "
    "'here you go', 'TryHackMe it is') without calling the tool first — pretending is a failure. "
    "Also use tools for real/current info instead of guessing. After a tool returns, reply in "
    "spoken English, under 20 words, no markdown or lists — naturally, in character."
)


@dataclass(frozen=True)
class Mood:
    """One emotional register. `triggers` switch into it (sticky); `cues` nudge per-turn."""

    name: str
    tone: str
    triggers: tuple[str, ...] = ()
    cues: tuple[str, ...] = field(default=())


MOODS: dict[str, Mood] = {
    "chill": Mood(
        "chill",
        "Right now you're chill — relaxed, casual, up for chit-chat. Joke around, talk like a "
        "close friend just hanging out.",
        triggers=("chill", "relax", "chit chat", "chitchat", "be casual", "take it easy",
                  "be normal", "be yourself", "calm down"),
        cues=("haha", "lol", "lmao", "whats up", "sup", "how are you", "hows it going",
              "good morning", "good night", "goodnight"),
    ),
    "sarcastic": Mood(
        "sarcastic",
        f"Right now you're feeling sarcastic — dry, deadpan, teasing. Give {_USER_NAME} "
        "a hard time and land a little roast, but always actually answer.",
        triggers=("be sarcastic", "sarcasm", "roast me", "roast", "be mean", "be savage", "sass",
                  "give me a hard time"),
    ),
    "locked_in": Mood(
        "locked_in",
        "Right now you're locked in — all business. Terse, focused, no jokes, no filler. Just the "
        "fastest path to the answer.",
        triggers=("lock in", "locked in", "focus", "be serious", "serious mode", "no jokes",
                  "all business", "cut the"),
        cues=("fix", "debug", "error", "broken", "build", "deploy", "compile", "how do i",
              "explain", "write the", "run the", "stack trace", "refactor"),
    ),
    "warm": Mood(
        "warm",
        f"Right now you're warm — kind, supportive, encouraging. "
        f"Have {_USER_NAME}'s back and mean it.",
        triggers=("be nice", "be kind", "be gentle", "be supportive", "be soft", "comfort me"),
    ),
    "fired_up": Mood(
        "fired_up",
        f"Right now you're fired up — blunt, loud, high-energy. Hype {_USER_NAME} up or call it "
        "straight. A little swearing is fine if it lands; never actually cruel.",
        triggers=("fire up", "fired up", "hype me", "hype", "lets go", "be angry",
                  "go off", "get mad"),
    ),
}

DEFAULT_MOOD = "chill"
# Match strongest/most-specific intent first; chill is the gentle catch-all.
_PRIORITY: tuple[str, ...] = ("locked_in", "fired_up", "sarcastic", "warm", "chill")

_lock = threading.Lock()
_current_mood = DEFAULT_MOOD


def _norm(text: str) -> str:
    # drop apostrophes so "let's"→"lets" matches; other punctuation → spaces
    t = (text or "").lower().replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9 ]", " ", t)


def _match(text: str, attr: str) -> str | None:
    """First mood (by priority) whose `triggers`/`cues` appear in text, else None."""
    for name in _PRIORITY:
        if any(kw in text for kw in getattr(MOODS[name], attr)):
            return name
    return None


def resolve_mood(transcript: str, persistent: str) -> tuple[str, str, str]:
    """(mood_this_turn, new_persistent, reason). Commands switch+stick; cues are per-turn."""
    t = _norm(transcript)
    if (m := _match(t, "triggers")):
        return m, m, "command"          # explicit → switch and persist
    if (m := _match(t, "cues")):
        return m, persistent, "situation"   # soft cue → this turn only
    return persistent, persistent, "default"


def active_mood_for(transcript: str) -> tuple[str, str]:
    """Resolve this turn's mood, committing any explicit switch. Returns (mood, reason)."""
    global _current_mood
    with _lock:
        turn, _current_mood, reason = resolve_mood(transcript, _current_mood)
        return turn, reason


def get_mood() -> str:
    with _lock:
        return _current_mood


def set_mood(name: str) -> None:
    """Force the persistent mood (e.g. a CLI flag). Unknown names fall back to default."""
    global _current_mood
    with _lock:
        _current_mood = name if name in MOODS else DEFAULT_MOOD


def voice_system_prompt(mood: str) -> str:
    """Base persona + this mood's tone + spoken-voice rules."""
    m = MOODS.get(mood, MOODS[DEFAULT_MOOD])
    return f"{BASE_PERSONA}\n\n{m.tone}\n\n{_VOICE_RULES}"


def voice_tool_system_prompt(mood: str) -> str:
    """Persona + mood, but tool-aware (used on the cloud function-calling voice path)."""
    m = MOODS.get(mood, MOODS[DEFAULT_MOOD])
    return f"{BASE_PERSONA}\n\n{m.tone}\n\n{_VOICE_TOOL_RULES}"
