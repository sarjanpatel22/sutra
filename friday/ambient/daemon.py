"""Ambient memory daemon: polls collectors, flushes summaries into the graph.

Loop: every few seconds each collector observes cheaply; every ~20 minutes (and on
shutdown) the daemon drains them into natural-language episodes and writes those to
the Graphiti/FalkorDB memory store. Idle time (no keyboard/mouse) is not counted as
activity. Degrades gracefully — if memory is down, summaries are logged, not lost to
a crash.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from friday.ambient import config
from friday.ambient.collectors import build_collectors
from friday.memory.store import get_store, looks_low_value

_STATE_DIR = Path.home() / ".local" / "share" / "friday"
_STATE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(_STATE_DIR / "ambient.log"),
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger("sutra.ambient")


class _IdleTracker:
    """Tracks time since last input via pynput; off (never idle) if unavailable."""

    def __init__(self) -> None:
        self._last = time.monotonic()
        self._ok = False
        try:
            from pynput import keyboard, mouse

            def bump(*_a: object) -> None:
                self._last = time.monotonic()

            keyboard.Listener(on_press=bump).start()
            mouse.Listener(on_move=bump, on_click=bump, on_scroll=bump).start()
            self._ok = True
        except Exception as e:  # noqa: BLE001 — no input device / headless
            log.info("idle tracking disabled (%s) — assuming always active", e)

    @property
    def idle(self) -> bool:
        return self._ok and (time.monotonic() - self._last) > config.IDLE_THRESHOLD_S


class AmbientDaemon:
    def __init__(self) -> None:
        self.collectors = build_collectors()
        self._idle = _IdleTracker()

    def _poll(self) -> None:
        idle = self._idle.idle
        for c in self.collectors:
            try:
                c.poll(idle=idle)
            except Exception as e:  # noqa: BLE001
                log.warning("poll %s failed: %s", c.name, e)

    async def flush(self) -> int:
        """Drain collectors into episodes and write them to memory. Returns count."""
        episodes: list[tuple[str, str]] = []
        for c in self.collectors:
            try:
                episodes += [(c.name, e) for e in c.drain()]
            except Exception as e:  # noqa: BLE001
                log.warning("drain %s failed: %s", c.name, e)
        if not episodes:
            log.info("flush: nothing new")
            return 0

        store = get_store()
        available = await store.initialize()
        wrote = skipped = 0
        for name, text in episodes:
            # Drop activity telemetry / narration at the source so it never pollutes
            # recall (the cure for the 60%-noise graph; see store.looks_low_value).
            if looks_low_value(text):
                skipped += 1
                continue
            log.info("[%s] %s", name, text)
            if available and await store.remember(text, source=f"ambient:{name}"):
                wrote += 1
        if skipped:
            log.info("flush: skipped %d low-value episode(s)", skipped)
        # Reset only on a real write FAILURE (something was attempted but none stored),
        # not when every episode was skipped as low-value — otherwise initialize()'s
        # cached True would never get a chance and we'd needlessly reconnect each flush.
        attempted = len(episodes) - skipped
        if attempted and wrote == 0:
            store.reset()
            log.warning(
                "memory unavailable — %d episode(s) logged but not stored; "
                "will reconnect next flush", attempted
            )
        return wrote

    async def run_once(self) -> int:
        """One poll + flush — handy for testing (window needs the loop to accumulate)."""
        self._poll()
        return await self.flush()

    async def run(self) -> None:
        if not config.ENABLED:
            print("Ambient memory is disabled (AMBIENT_ENABLED=0).")
            return
        sources = ", ".join(c.name for c in self.collectors) or "none"
        dirs = ", ".join(str(d) for d in config.WATCH_DIRS) or "none"
        print("\n  🛰  Sutra ambient memory")
        print(f"      sources : {sources}")
        print(f"      watching: {dirs}")
        print(f"      flush   : every {config.FLUSH_INTERVAL_S // 60} min  ·  Ctrl+C to stop\n")
        log.info("ambient daemon started (sources=%s)", sources)

        last_flush = time.monotonic()
        try:
            while True:
                self._poll()
                if time.monotonic() - last_flush >= config.FLUSH_INTERVAL_S:
                    n = await self.flush()
                    last_flush = time.monotonic()
                    print(f"  · flushed {n} episode(s) to memory")
                await asyncio.sleep(config.POLL_INTERVAL_S)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n  flushing before exit…")
            n = await self.flush()
            print(f"  · flushed {n} episode(s). Stopped.")
            log.info("ambient daemon stopped")
