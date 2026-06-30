"""Fast screen perception skill — locate a target on screen quickly and reliably.

find_on_screen grabs the live screen with mss (≈50 fps) and locates a target two ways: a TEMPLATE
image (find an exact button/icon via OpenCV — instant, reliable) or TEXT (region-clean OCR). It
returns precise coordinates to click, plus a count of detected clickable elements when the target
isn't found, so the agent knows what's actually on screen. This is the fast replacement for the slow
full-screen-OCR path. Read-only (it only looks) → auto_confirm. Needs a display; $0.
"""

from __future__ import annotations

from typing import Any

from friday.skills.registry import skill


@skill(
    name="find_on_screen",
    description=(
        "Quickly locate a target on the live screen and return where to click. Pass text (located "
        "via fast region-clean OCR) or template_path to an image of a button/icon (located via "
        "OpenCV template matching — instant and exact). Returns the centre coordinates to click, "
        "the match score, and — if not found — how many clickable elements are on screen. The "
        "fast, reliable replacement for slow full-screen OCR. Read-only (it only looks)."
    ),
    auto_confirm=True,  # read-only perception — only looks at the screen
    timeout_s=20,
)
async def find_on_screen(target: str = "", template_path: str = "") -> dict[str, Any]:
    """Locate `target` text (or a `template_path` image) on the live screen.

    target: text to find on screen (e.g. "Download configuration file").
    template_path: path to a button/icon image to find by template matching (overrides target).
    """
    import asyncio

    import cv2

    from friday.vision import perceive as P

    if not target.strip() and not template_path.strip():
        return {"error": "give a target text or a template_path"}

    def _run() -> dict[str, Any]:
        try:
            screen = P.grab()
        except Exception as e:  # noqa: BLE001 — no display / mss failure
            return {"error": f"could not capture the screen: {str(e)[:120]}"}

        if template_path.strip():
            tpl = cv2.imread(template_path)
            if tpl is None:
                return {"error": f"could not read template image: {template_path}"}
            m = P.find_template(screen, tpl)
            how = "template"
        else:
            m = P.locate_text(screen, target)
            how = "text"

        if m is not None:
            cx, cy = m.center
            return {"found": True, "method": how, "x": cx, "y": cy,
                    "box": [m.x, m.y, m.w, m.h], "score": round(m.score, 3),
                    "note": f"Found '{target or template_path}' at ({cx},{cy}) — click there."}
        elements = P.detect_elements(screen)
        return {"found": False, "method": how,
                "clickable_elements": len(elements),
                "note": (f"'{target or template_path}' not found. {len(elements)} clickable "
                         "element(s) detected — refine the target text or pass a template image.")}

    return await asyncio.to_thread(_run)
