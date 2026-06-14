"""Desktop take-over: find the on-screen control for a goal and actually click it.

The "…then take the order" half of guide_me — guidance that ACTS. Locates text
controls by OCR (friday.vision.locate, real coordinates) and clicks/types via xdotool.
IRREVERSIBLE (moves the mouse, clicks the live desktop) so both skills are confirmation-
gated. Reliability comes from OCR ground-truth coordinates, not pixel-guessing; honest
limit: text-labelled controls only — if nothing matches it reports what it CAN see.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from typing import Any

from friday.skills.registry import skill
from friday.vision.locate import Element, ocr_available, ocr_screen

_XDOTOOL = shutil.which("xdotool")

# Words that describe the ACTION/wrapper, not the target label — stripped before matching.
_FILLER = re.compile(
    r"\b(?:click|clicks|clicking|press|pressing|tap|hit|push|select|choose|open|opens|"
    r"the|a|an|on|onto|button|btn|menu|icon|tab|link|option|item|field|please|for|me|go|to)\b",
    re.I,
)


def _core(goal: str) -> str:
    """Reduce a goal to its target words ('click the Search menu' → 'search')."""
    return " ".join(_FILLER.sub(" ", goal.lower()).split())


def _score(target: str, el: Element) -> float:
    """How well an OCR'd element matches the target words (0 = no match)."""
    t = el.text.lower().strip()
    if not t or not target:
        return 0.0
    conf = el.conf / 100.0
    if t == target:
        return 3.0 + conf
    tgt_tokens = set(target.split())
    if t in tgt_tokens:  # element text is one of the target words
        return 2.5 + conf
    if target in t or t in target:  # substring either direction
        return 2.0 + conf
    overlap = tgt_tokens & set(t.split())
    if overlap:  # share at least one word
        return 1.0 + len(overlap) / max(len(tgt_tokens), 1) + conf
    return 0.0


def best_match(goal: str, elements: list[Element], min_score: float = 1.5) -> Element | None:
    """Pick the highest-scoring element for the goal, or None if nothing's confident."""
    target = _core(goal)
    best: Element | None = None
    best_s = min_score
    for el in elements:
        s = _score(target, el)
        if s > best_s:
            best, best_s = el, s
    return best


async def _run(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()


async def click_xy(x: int, y: int, button: int = 1) -> None:
    """Move the pointer to (x, y) and click — the actuator."""
    if not _XDOTOOL:
        raise RuntimeError("xdotool not installed")
    await _run([_XDOTOOL, "mousemove", str(x), str(y), "click", str(button)])


async def type_text(text: str) -> None:
    """Type a string into the currently focused control."""
    if not _XDOTOOL:
        raise RuntimeError("xdotool not installed")
    await _run([_XDOTOOL, "type", "--clearmodifiers", text])


def _seen(elements: list[Element], n: int = 8) -> str:
    """A short list of the most confident labels visible — for honest 'no match' replies."""
    top = sorted(elements, key=lambda e: e.conf, reverse=True)[:n]
    return ", ".join(repr(e.text) for e in top) or "(no text found)"


async def _llm_pick(goal: str, elements: list[Element]) -> Element | None:
    """Semantic fallback when no label matches literally ('save my work' → 'Save').

    Lazy-imports the router (keeps module import cheap). Asks a free model for the
    best-matching index; degrades to None on ANY failure (no key, offline, junk
    reply) so the caller falls back to the honest 'no match', never a crash.
    """
    try:
        from friday.core.router import Tier, complete

        listing = "\n".join(f"{i}: {el.text}" for i, el in enumerate(elements))
        instruction = (
            "Pick which on-screen label best fulfils the user's goal. "
            "Reply with ONLY the number, or -1 if none fit."
        )
        response = await complete(
            [
                {"role": "system", "content": instruction},
                {"role": "user", "content": f"Goal: {goal}\nLabels:\n{listing}"},
            ],
            tier=Tier.MEDIUM,
            tools=None,
            max_tokens=8,
            temperature=0.0,
        )
        raw = (response.choices[0].message.content or "").strip()
        m = re.search(r"-?\d+", raw)
        if not m:
            return None
        idx = int(m.group())
        return elements[idx] if 0 <= idx < len(elements) else None
    except Exception:  # noqa: BLE001 — fallback must never crash the turn
        return None


async def _locate(goal: str) -> tuple[Element, str] | dict[str, Any]:
    """OCR the screen, find the control for `goal` (shared by both skills); returns
    `(element, matched_by='label'|'llm')` or an honest dict (error / 'nothing matched').
    """
    if not goal:
        return {"error": "Tell me what to act on."}
    if not ocr_available() or not _XDOTOOL:
        return {"error": "Desktop control needs tesseract + xdotool installed."}
    try:
        elements = await ocr_screen()
    except Exception as e:  # noqa: BLE001 — degrade honestly, never crash the turn
        return {"error": f"I couldn't read the screen: {str(e)[:120]}"}
    if not elements:
        return {"error": "I couldn't read any text controls on the screen."}
    el = best_match(goal, elements)
    if el is not None:
        return el, "label"
    el = await _llm_pick(goal, elements)  # semantic fallback only when literal fails
    if el is not None:
        return el, "llm"
    return {
        "acted": False,
        "goal": goal,
        "note": f"Nothing clearly matched '{goal}'. I can see: {_seen(elements)}.",
    }


@skill(
    name="act_on_screen",
    description=(
        "Actually CLICK a control on the user's real desktop — the take-over step "
        "after guidance. Give the goal/label (e.g. 'click the Search menu', 'press "
        "Save'). Finds the control by its on-screen text and clicks its real location. "
        "Text-labelled controls only. IRREVERSIBLE — requires confirmation."
    ),
    auto_confirm=False,
    timeout_s=30,
)
async def act_on_screen(goal: str) -> dict[str, Any]:
    """Locate the control matching `goal` by OCR and click it. Honest when unsure."""
    found = await _locate(goal.strip())
    if isinstance(found, dict):
        return found  # honest error / no-match
    el, matched_by = found
    await click_xy(el.cx, el.cy)
    return {
        "acted": True,
        "goal": goal.strip(),
        "target": el.text,
        "coords": [el.cx, el.cy],
        "matched_by": matched_by,
        "note": f"Clicked '{el.text}'.",
    }


@skill(
    name="type_into",
    description="Click a text field found by its on-screen label/placeholder, then "
    "type text into it. IRREVERSIBLE — requires confirmation.",
    auto_confirm=False,
    timeout_s=30,
)
async def type_into(field: str, text: str) -> dict[str, Any]:
    """Find a text field by its label/placeholder, click it, and type into it."""
    found = await _locate(field.strip())
    if isinstance(found, dict):
        return found  # honest error / no-match
    el, _ = found
    await click_xy(el.cx, el.cy)
    await type_text(text)
    return {"typed": True, "field": field.strip(), "into": el.text, "text": text}
