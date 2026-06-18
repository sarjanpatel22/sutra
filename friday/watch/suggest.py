"""Turn one desktop screenshot into a single proactive suggestion — or SILENT.

This is the "brain" of `sutra watch`. It sends the screenshot to a free Groq
vision model (Llama-4-Scout, $0) behind a heavily silence-biased prompt: the
model is told that the right answer is almost always SILENT, and to speak up
ONLY when there's a genuinely useful, specific, non-obvious next step. We then
parse defensively — anything that's SILENT / empty / pure punctuation collapses
to `None`, so the loop surfaces nothing.
"""

from __future__ import annotations

import base64
import re

import litellm

from friday.core.constants import GROQ_VISION_MODEL

DEFAULT_MODEL = GROQ_VISION_MODEL

_SYS = (
    "You are Sutra, quietly watching Sarjan's screen to help him proactively, "
    "like a sharp pair-programming partner glancing over. Look at the screenshot. "
    "ONLY if there is a GENUINELY useful, specific, non-obvious next step, fix, "
    "shortcut, or thing he's missing or doing inefficiently, reply with ONE short "
    "suggestion (max ~18 words, spoken-style, no markdown). In EVERY other case "
    "reply with exactly the single word: SILENT. Do NOT state the obvious, do NOT "
    "narrate what he's already doing, do NOT comment on UI chrome, do NOT suggest "
    "just to say something. When in doubt, reply SILENT. Most of the time the right "
    "answer is SILENT."
)

_USER = (
    "Here is Sarjan's current screen. Give one genuinely useful suggestion, or "
    "reply SILENT."
)

# Stuck mode: fired when he's been on the SAME screen a while. Bias toward a concrete
# next step (not silence) IF he looks stuck — but still SILENT when he's clearly fine.
_SYS_STUCK = (
    "You are Sutra, watching Sarjan's screen. He has been on this SAME screen for a while "
    "and may be STUCK. Look at the screenshot. If he genuinely looks stuck — an error or "
    "traceback, a failing command, a test that won't pass, a form he can't get past, or a "
    "problem with no obvious next move — reply with ONE concrete, specific thing to try next "
    "(max ~22 words, spoken-style, no markdown), like a sharp teammate leaning over. If he "
    "looks fine — reading, watching, thinking, or just idle — reply with exactly: SILENT. "
    "When unsure, reply SILENT."
)
_USER_STUCK = (
    "Sarjan has been on this screen a while. If he looks stuck, give ONE concrete next thing "
    "to try; otherwise reply SILENT."
)

# Leading "SILENT" the model sometimes prepends before talking anyway.
_LEAD_SILENT = re.compile(r"^\s*silent\b[\s:.,;!-]*", re.IGNORECASE)
# A reply that is only whitespace/punctuation carries no suggestion.
_ONLY_PUNCT = re.compile(r"^[\s\W_]*$")


def clean_suggestion(text: str | None) -> str | None:
    """Normalise a raw model reply into a suggestion string, or None for SILENT.

    Returns None when the reply is empty, equals SILENT (case-insensitive), or is
    just punctuation. Strips a leading "SILENT" if the model prepended it and then
    kept talking, and trims surrounding quotes/markdown emphasis.
    """
    if not text:
        return None
    s = text.strip()
    if s.lower() == "silent":
        return None
    s = _LEAD_SILENT.sub("", s).strip()
    s = s.strip("\"'`*_ ").strip()
    if not s or _ONLY_PUNCT.match(s):
        return None
    return s


async def suggest_from_screen(
    jpeg_bytes: bytes, model: str = DEFAULT_MODEL, *, stuck: bool = False
) -> str | None:
    """Ask the vision model for one suggestion about this screenshot, or None.

    `jpeg_bytes` come from `friday.vision.screen.grab_jpeg()`. `stuck=True` swaps in the
    "you've been here a while — are you stuck?" prompt (used when the screen has dwelled).
    Any model/transport error is allowed to propagate; the loop wraps the call so a failed
    tick never crashes the watcher.
    """
    shot = base64.b64encode(jpeg_bytes).decode()
    r = await litellm.acompletion(
        model=model,
        messages=[
            {"role": "system", "content": _SYS_STUCK if stuck else _SYS},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _USER_STUCK if stuck else _USER},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{shot}"},
                    },
                ],
            },
        ],
        max_tokens=120,
        temperature=0.3,
    )
    return clean_suggestion(r.choices[0].message.content)
