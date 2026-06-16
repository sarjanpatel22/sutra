"""`guide_me` — Sutra looks at Sarjan's ACTUAL desktop and says which control to press.

GUIDANCE ONLY. This skill is strictly read-only: it grabs one screenshot, asks a
free vision model where the relevant button/menu/field is, and reports that in a
short spoken-friendly sentence. It NEVER clicks, types, or moves anything — desktop
auto-clicking is a deliberate, separate next step, not part of this.

Privacy note: the screenshot is sent to Groq's hosted vision API (Llama-4-Scout) to
locate the control. Nothing is stored; the image leaves the machine only for that
single inference call.
"""

from __future__ import annotations

from typing import Any

import litellm

from friday.core.constants import GROQ_VISION_MODEL
from friday.skills.registry import skill
from friday.vision.screen import ScreenUnavailable, grab_jpeg, screen_available

_VISION_MODEL = GROQ_VISION_MODEL  # free, fast, multimodal

_SYS = (
    "You are Sutra, looking at Sarjan's actual desktop screenshot to guide him. Given "
    "his GOAL, find the SPECIFIC on-screen control he should use and tell him in ONE or "
    "two short spoken sentences what to click and where it is (e.g. 'Click the green "
    "Deploy button in the top-right toolbar.'). Be concrete about location. If you "
    "genuinely can't see anything relevant to the goal, say so briefly and suggest where "
    "he might look. No markdown, no lists, no preamble."
)


def _encode(jpeg_bytes: bytes) -> str:
    """Base64-encode JPEG bytes for inline data-URL embedding in the vision request."""
    import base64

    return base64.b64encode(jpeg_bytes).decode()


@skill(
    name="guide_me",
    description=(
        "Look at the user's ACTUAL desktop screen and tell them exactly which "
        "button, menu, field, or control to click to accomplish a goal. Use when they ask "
        "'how do I…', 'where is the button for…', 'show me how to…', or 'which button do I "
        "press' about something on their screen right now. Read-only — it only looks and advises."
    ),
    auto_confirm=True,  # read-only: it just looks and describes, performs no action
    timeout_s=60,
)
async def guide_me(goal: str) -> dict[str, Any]:
    """Point Sarjan at the on-screen control for `goal`, by looking at his real screen.

    Args:
        goal: What he's trying to do (e.g. "save this file", "start the VPN").
    """
    goal = goal.strip()
    if not goal:
        return {"error": "Tell me what you're trying to do."}
    if not screen_available():
        return {"error": "I can't see your screen right now."}

    try:
        jpeg = await grab_jpeg()
    except ScreenUnavailable:
        return {"error": "I can't see your screen right now."}

    shot = _encode(jpeg)
    try:  # free Groq vision; called directly (router has no multimodal tier)
        r = await litellm.acompletion(
            model=_VISION_MODEL,
            messages=[
                {"role": "system", "content": _SYS},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"GOAL: {goal}\n\n"
                            "Look at my screen and tell me what to click.",
                        },
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
        return {"error": "I looked, but couldn't tell what to point you at."}
    return {"goal": goal, "guidance": text}
