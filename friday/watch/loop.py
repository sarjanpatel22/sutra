"""The always-on "JARVIS" watch loop — Sutra glances at the desktop and, only
when it genuinely helps, surfaces ONE proactive suggestion.

How it stays useful instead of annoying:
  • It only looks when the screen actually CHANGED (cheap average-hash gate), so a
    static/idle screen costs $0 and burns no vision quota.
  • A cooldown spaces suggestions out; a daily cap bounds them; a rolling recent
    list drops near-duplicates.
  • The vision prompt is heavily silence-biased — most ticks return SILENT and
    nothing is shown.
  • Every tick is wrapped: a capture/vision/notify failure is logged and the loop
    sleeps and continues. It NEVER crashes.

The gating is a pure function (`decide_action`) so it's unit-testable without any
capture, network, or notification I/O.

Config (env vars, read at startup; defaults in parens):
  WATCH_POLL_S (20)            seconds between ticks
  WATCH_COOLDOWN_S (90)        minimum seconds between two surfaced suggestions
  WATCH_CHANGE_THRESHOLD (8)   ahash hamming distance that counts as "changed"
  WATCH_DAILY_CAP (40)         max suggestions per day (resets at local midnight)
  WATCH_SPEAK (0)              if truthy, also speak the suggestion via spd-say
  WATCH_VISION_MODEL           free Groq vision model id

Privacy: this loop sends desktop screenshots to Groq's API (the free vision model)
ONLY when the screen changes and the cooldown has elapsed. It is OFF by default and
runs only when Sarjan explicitly starts `sutra watch`. Screenshots are never saved
to disk.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from datetime import date

from friday.core import monitor
from friday.vision import screen
from friday.watch.state import WatchConfig, WatchState
from friday.watch.suggest import suggest_from_screen

log = logging.getLogger("sutra.watch")

_RECENT_KEEP = 5  # how many past suggestions we compare against for dedupe

SuggestFn = Callable[[bytes, str], Awaitable[str | None]]
NotifyFn = Callable[[str, str], None]


def _norm(s: str) -> str:
    """Case-insensitive, whitespace-collapsed form for cheap dedupe equality."""
    return " ".join(s.lower().split())


def _roll_day(state: WatchState, today: date) -> None:
    """Reset the daily counter when the local date has rolled over."""
    if state.day != today:
        state.day = today
        state.count_today = 0


def decide_action(state: WatchState, cur_hash: int, now: float, cfg: WatchConfig) -> str:
    """Decide what this tick should do — pure, no I/O.

    Returns one of:
      "skip_no_change" — screen is static / user idle (don't even look)
      "skip_cooldown"  — changed, but too soon since the last suggestion
      "skip_cap"       — changed, but today's suggestion cap is reached
      "look"           — changed, past cooldown, under cap → grab + ask the model

    Rolls the daily counter over at midnight as a side effect on `state`.
    """
    _roll_day(state, date.fromtimestamp(now))
    if not screen.changed(state.last_hash, cur_hash, cfg.change_threshold):
        return "skip_no_change"
    if now - state.last_suggest_ts < cfg.cooldown_s:
        return "skip_cooldown"
    if state.count_today >= cfg.daily_cap:
        return "skip_cap"
    return "look"


def _is_duplicate(state: WatchState, suggestion: str) -> bool:
    """True if this suggestion matches one of the last few surfaced ones."""
    norm = _norm(suggestion)
    return any(_norm(prev) == norm for prev in state.recent[-_RECENT_KEEP:])


def _speak(text: str) -> None:
    """Best-effort TTS via spd-say; never raises if the binary is missing/fails."""
    if not shutil.which("spd-say"):
        return
    try:
        subprocess.run(["spd-say", "--", text], check=False, timeout=3)
    except Exception:  # noqa: BLE001 — speaking is a nicety, never fatal
        log.debug("spd-say failed", exc_info=True)


async def tick(
    state: WatchState,
    cfg: WatchConfig,
    now: float,
    *,
    suggest_fn: SuggestFn = suggest_from_screen,
    notify_fn: NotifyFn = monitor.notify,
) -> str | None:
    """Run one watch tick. Returns the surfaced suggestion, or None if quiet.

    `suggest_fn`/`notify_fn` are injectable so tests can exercise the full tick
    with no capture, network, or desktop notification.
    """
    cur = await screen.ahash()
    action = decide_action(state, cur, now, cfg)
    if action != "look":
        state.last_hash = cur
        return None

    surfaced: str | None = None
    try:
        jpeg = await screen.grab_jpeg()
        suggestion = await suggest_fn(jpeg, cfg.vision_model)
        if suggestion and not _is_duplicate(state, suggestion):
            notify_fn("Sutra", suggestion)
            if cfg.speak:
                _speak(suggestion)
            state.last_suggest_ts = now
            state.count_today += 1
            state.recent.append(suggestion)
            del state.recent[:-_RECENT_KEEP]
            surfaced = suggestion
    except screen.ScreenUnavailable as e:
        log.debug("watch tick: screen unavailable — %s", e)
    except Exception:  # noqa: BLE001 — a bad tick must never kill the loop
        log.warning("watch tick failed", exc_info=True)
    finally:
        state.last_hash = cur
    return surfaced


async def watch_once(cfg: WatchConfig | None = None) -> str | None:
    """Run exactly ONE watch tick and return its surfaced suggestion (or None).

    A single-shot for testing `sutra watch --once`: fresh state, current wall clock,
    one `tick`. Returns None when the screen is unavailable, unchanged, or the model
    stays quiet. Does not loop and does not touch `run_watch`'s behaviour.
    """
    cfg = cfg or WatchConfig.from_env()
    if not screen.screen_available():
        log.warning("watch --once: no display / no screenshot tool — nothing to watch")
        return None
    return await tick(WatchState(), cfg, _now())


async def run_watch(cfg: WatchConfig | None = None) -> None:
    """Entry point for `sutra watch`: loop forever, ticking every `poll_s` seconds.

    OFF by default — only called when Sarjan explicitly starts the watcher. Exits
    cleanly on cancellation; otherwise runs until interrupted.
    """
    cfg = cfg or WatchConfig.from_env()
    if not screen.screen_available():
        log.warning("watch: no display / no screenshot tool — nothing to watch")
        return
    state = WatchState()
    log.info("watch started — polling every %.0fs (cap %d/day)", cfg.poll_s, cfg.daily_cap)
    try:
        while True:
            await tick(state, cfg, _now())
            await asyncio.sleep(cfg.poll_s)
    except asyncio.CancelledError:
        log.info("watch stopped")
        raise


def _now() -> float:
    """Wall-clock seconds (separate fn so it's trivial to monkeypatch in tests)."""
    import time

    return time.time()
