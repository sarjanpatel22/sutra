"""The always-on "JARVIS" watch loop — Sutra glances at the desktop and, only
when it genuinely helps, surfaces ONE proactive suggestion.

How it stays useful instead of annoying:
  • It looks when the screen CHANGED (cheap average-hash gate) — and ALSO when you've
    dwelled on the SAME screen too long (WATCH_DWELL_S), which is how it catches you
    being STUCK (staring at an error) rather than only reacting to activity.
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
  WATCH_DWELL_S (60)           seconds on the SAME screen before checking "are you stuck?"
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

SuggestFn = Callable[..., Awaitable[str | None]]  # (jpeg, model, *, stuck=False) -> suggestion
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
      "skip_no_change" — static screen, not dwelled long enough to look "stuck"
      "skip_cooldown"  — would act, but too soon since the last suggestion
      "skip_cap"       — would act, but today's suggestion cap is reached
      "look"           — screen CHANGED → grab + ask for a proactive suggestion
      "stuck"          — screen UNCHANGED for >= dwell_s → grab + ask "are you stuck?"

    Rolls the daily counter over at midnight as a side effect on `state`.
    """
    _roll_day(state, date.fromtimestamp(now))
    if screen.changed(state.last_hash, cur_hash, cfg.change_threshold):
        state.last_change_ts = now  # fresh activity — reset the dwell clock + stuck latch
        state.stuck_hash = None
        return _gate(state, now, cfg, "look")
    # Screen UNCHANGED — has he been parked here long enough to maybe be stuck?
    if state.last_change_ts == 0.0:
        state.last_change_ts = now  # first static observation — start the dwell clock
        return "skip_no_change"
    if cfg.dwell_s <= 0 or now - state.last_change_ts < cfg.dwell_s:
        return "skip_no_change"
    if state.stuck_hash == cur_hash:
        return "skip_no_change"  # already offered help for this exact stuck screen
    return _gate(state, now, cfg, "stuck")


def _gate(state: WatchState, now: float, cfg: WatchConfig, action: str) -> str:
    """Apply the shared cooldown + daily-cap gate to a would-be action."""
    if now - state.last_suggest_ts < cfg.cooldown_s:
        return "skip_cooldown"
    if state.count_today >= cfg.daily_cap:
        return "skip_cap"
    return action


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
    if action not in ("look", "stuck"):
        state.last_hash = cur
        return None

    surfaced: str | None = None
    try:
        jpeg = await screen.grab_jpeg()
        if action == "stuck":
            suggestion = await suggest_fn(jpeg, cfg.vision_model, stuck=True)
        else:
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
        if action == "stuck":
            state.stuck_hash = cur  # don't re-ask about this same stuck screen
    return surfaced


async def watch_once(
    cfg: WatchConfig | None = None, *, suggest_fn: SuggestFn = suggest_from_screen
) -> str | None:
    """Run exactly ONE watch tick and return its surfaced suggestion (or None).

    A single-shot for testing `sutra watch --once`: fresh state, current wall clock,
    one `tick`. Returns None when the screen is unavailable, unchanged, or the model
    stays quiet. Does not loop and does not touch `run_watch`'s behaviour.
    `suggest_fn` swaps the suggester (e.g. the pentest tutor for `watch --tutor`)."""
    cfg = cfg or WatchConfig.from_env()
    if not screen.screen_available():
        log.warning("watch --once: no display / no screenshot tool — nothing to watch")
        return None
    return await tick(WatchState(), cfg, _now(), suggest_fn=suggest_fn)


async def run_watch(
    cfg: WatchConfig | None = None, *, suggest_fn: SuggestFn = suggest_from_screen
) -> None:
    """Entry point for `sutra watch`: loop forever, ticking every `poll_s` seconds.

    OFF by default — only called when Sarjan explicitly starts the watcher. Exits
    cleanly on cancellation; otherwise runs until interrupted. `suggest_fn` swaps the
    suggester — `watch --tutor` passes the pentest tutor so a stall surfaces a coaching hint."""
    cfg = cfg or WatchConfig.from_env()
    if not screen.screen_available():
        log.warning("watch: no display / no screenshot tool — nothing to watch")
        return
    state = WatchState()
    log.info("watch started — polling every %.0fs (cap %d/day)", cfg.poll_s, cfg.daily_cap)
    try:
        while True:
            await tick(state, cfg, _now(), suggest_fn=suggest_fn)
            await asyncio.sleep(cfg.poll_s)
    except asyncio.CancelledError:
        log.info("watch stopped")
        raise


def _now() -> float:
    """Wall-clock seconds (separate fn so it's trivial to monkeypatch in tests)."""
    import time

    return time.time()
