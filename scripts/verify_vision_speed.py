"""Real end-to-end before/after for web_task_vision, in an ISOLATED headless browser.

Monkeypatches the browser profile dir + forces headless so the REAL skill runs
without colliding with the live voice session's browser. Prints total time, step
count, and the answer for each task — run it before and after the optimization to
get a true before/after (not a guess).

Run: .venv/bin/python scripts/verify_vision_speed.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path

os.environ["SUTRA_BROWSER_HEADLESS"] = "1"  # isolated, invisible — don't touch his window

from friday.skills import browser_session as bs  # noqa: E402

bs._PROFILE_DIR = Path(tempfile.mkdtemp(prefix="sutra-vision-prof-"))  # isolated profile

from friday.skills import vision_agent as va  # noqa: E402

TASKS = [
    ("what year was the Linux kernel first released", "https://en.wikipedia.org/wiki/Linux_kernel"),
    ("what is the capital of France", "https://en.wikipedia.org/wiki/France"),
]


async def main() -> None:
    print("=== web_task_vision — real end-to-end (isolated headless) ===\n")
    for goal, url in TASKS:
        t = time.monotonic()
        try:
            res = await va.web_task_vision(goal, url, max_steps=8)
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] {goal!r}: {type(exc).__name__}: {exc}")
            continue
        dt = time.monotonic() - t
        per = dt / res.get("steps") if res.get("steps") else dt
        ans = str(res.get("answer", res.get("error", "")))[:90]
        print(f"[{dt:5.1f}s total | {per:4.1f}s/step | steps={res.get('steps')}] {goal!r}")
        print(f"         -> answer={ans!r}\n")
    await bs.close_session()


if __name__ == "__main__":
    asyncio.run(main())
