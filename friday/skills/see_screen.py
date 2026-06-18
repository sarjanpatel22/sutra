"""`see_screen` — Sutra looks at Sarjan's actual desktop and describes what's on it.

The companion to guide_me: where guide_me answers "which control do I click for GOAL",
this answers the plain "what do you see / what's on my screen right now" — Sutra grabs one
screenshot, asks a free vision model to describe it, and reports back in a short spoken
sentence. Strictly read-only: it only looks, never clicks or types.

Privacy note: the screenshot is sent to Groq's hosted vision API (Llama-4-Scout) for that
single inference. Nothing is stored; the image leaves the machine only for that call.
"""

from __future__ import annotations

import base64
from typing import Any

import litellm

from friday.core.constants import GROQ_VISION_MODEL
from friday.skills.registry import skill
from friday.vision.screen import ScreenUnavailable, grab_jpeg, screen_available

_VISION_MODEL = GROQ_VISION_MODEL  # free, fast, multimodal

_SYS = (
    "You are Sutra, looking at Sarjan's actual desktop screenshot. Describe what's on the "
    "screen right now in ONE or two short, natural spoken sentences: the app or window in "
    "focus, and the few things that matter most (what he's doing, any obvious dialog, error, "
    "or notification). Be concrete but brief. If a QUESTION is given, answer it from what you "
    "see. No markdown, no lists, no preamble."
)


@skill(
    name="see_screen",
    description=(
        "Look at the user's ACTUAL desktop screen right now and describe what's on it. "
        "Use when they ask 'what do you see', 'what's on my screen', 'look at my screen', "
        "'read/describe my screen', or ask a question about what's currently displayed. "
        "Read-only — it only looks and describes, performs no action."
    ),
    auto_confirm=True,  # read-only: it just looks and describes, performs no action
    timeout_s=60,
)
async def see_screen(question: str = "") -> dict[str, Any]:
    """Describe Sarjan's real desktop screen, optionally answering a question about it.

    Args:
        question: Optional specific question about what's on screen (e.g. "is the
            scan done?"). Empty for a general description.
    """
    if not screen_available():
        return {"error": "I can't see your screen right now."}

    try:
        jpeg = await grab_jpeg()
    except ScreenUnavailable:
        return {"error": "I can't see your screen right now."}

    shot = base64.b64encode(jpeg).decode()
    prompt = (
        f"Question about my screen: {question.strip()}\n\nLook and answer."
        if question.strip()
        else "Look at my screen and tell me what you see."
    )
    try:  # free Groq vision; called directly (router has no multimodal tier)
        r = await litellm.acompletion(
            model=_VISION_MODEL,
            messages=[
                {"role": "system", "content": _SYS},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{shot}"},
                        },
                    ],
                },
            ],
            max_tokens=150,
            temperature=0.2,
        )
    except Exception as e:  # noqa: BLE001 — degrade honestly instead of crashing the turn
        return {"error": f"I couldn't read the screen: {str(e)[:160]}"}

    text = (r.choices[0].message.content or "").strip()
    if not text:
        return {"error": "I looked, but couldn't make out what's on the screen."}
    return {"description": text}
