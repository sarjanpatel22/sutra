"""Profile the vision pipeline in an ISOLATED browser (doesn't touch the live session).

Measures, per step: the networkidle wait, the Set-of-Mark JS, the screenshot, and
the Groq vision call — and A/Bs the biggest lever, full-PNG vs downscaled-JPEG
screenshots (bytes + vision-call latency). Measure first, optimize second.

Run: .venv/bin/python scripts/profile_vision.py
"""

from __future__ import annotations

import asyncio
import base64
import time

import litellm
from playwright.async_api import async_playwright

from friday.skills import vision_agent as va

GOAL = "what year was the Linux kernel first released"
URL = "https://en.wikipedia.org/wiki/Linux_kernel"


async def _vision_call(shot_b64: str, mime: str, listing: str) -> tuple[float, str]:
    user = [
        {"type": "text", "text": f"GOAL: {GOAL}\n\nNumbered elements:\n{listing}\n\n"
                                 "Look at the screenshot and reply with one JSON action:"},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{shot_b64}"}},
    ]
    t = time.monotonic()
    r = await litellm.acompletion(
        model=va._VISION_MODEL,
        messages=[{"role": "system", "content": va._SYS}, {"role": "user", "content": user}],
        max_tokens=200, temperature=0.1,
    )
    return time.monotonic() - t, str(va._parse(r.choices[0].message.content or ""))


async def main() -> None:
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"]
    )
    page = await browser.new_page(viewport={"width": 1366, "height": 880})

    t = time.monotonic()
    await page.goto(URL, wait_until="domcontentloaded", timeout=20000)
    print(f"goto(domcontentloaded) = {time.monotonic() - t:.2f}s")

    # 1) the networkidle wait the loop pays EVERY step
    t = time.monotonic()
    try:
        await page.wait_for_load_state("networkidle", timeout=3500)
    except Exception:  # noqa: BLE001
        pass
    print(f"networkidle wait(3500) = {time.monotonic() - t:.2f}s  <-- paid EACH step")

    # 2) Set-of-Mark JS
    t = time.monotonic()
    marks = await va._mark(page, cap=40)
    print(f"mark JS = {time.monotonic() - t:.2f}s  ({len(marks)} marks)")
    listing = "\n".join(f"{m['idx']}: <{m['tag']}> {m['label']}" for m in marks) or "(none)"

    # 3a) full-page PNG (current behaviour)
    t = time.monotonic()
    png = await page.screenshot()
    t_png = time.monotonic() - t
    print(f"\nPNG  screenshot = {t_png:.2f}s  size={len(png)/1024:.0f}KB")

    # 3b) downscaled JPEG (viewport-only, q50) — the candidate
    t = time.monotonic()
    jpg = await page.screenshot(type="jpeg", quality=50)
    t_jpg = time.monotonic() - t
    print(f"JPEG screenshot = {t_jpg:.2f}s  size={len(jpg)/1024:.0f}KB  "
          f"({len(png)/max(1, len(jpg)):.1f}x smaller)")

    await page.evaluate(va._CLEAR_JS)

    # 4) vision call latency for each image format (same page, same marks)
    v_png, act_png = await _vision_call(base64.b64encode(png).decode(), "image/png", listing)
    v_jpg, act_jpg = await _vision_call(base64.b64encode(jpg).decode(), "image/jpeg", listing)
    print(f"\nvision call (PNG  {len(png)/1024:.0f}KB) = {v_png:.2f}s  -> {act_png}")
    print(f"vision call (JPEG {len(jpg)/1024:.0f}KB) = {v_jpg:.2f}s  -> {act_jpg}")

    png_step = 3.5 + t_png + v_png
    jpg_step = 0.8 + t_jpg + v_jpg
    print(f"\n~per-step est:  current(PNG+3.5s wait)≈{png_step:.1f}s   "
          f"optimized(JPEG+0.8s wait)≈{jpg_step:.1f}s")

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
