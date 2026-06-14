"""Tests for the `sutra watch` loop — capture, vision, and notify are all mocked.

Nothing real is grabbed, nothing hits the network, and no desktop notification
fires. We exercise the pure gating (`decide_action`), the full tick via injected
suggest/notify functions, dedupe, midnight rollover, and the suggestion parser.
"""

from __future__ import annotations

import datetime as dt

import pytest

import friday.watch.loop as loop
import friday.watch.suggest as suggest_mod
from friday.watch.loop import decide_action, tick
from friday.watch.state import WatchConfig, WatchState
from friday.watch.suggest import clean_suggestion, suggest_from_screen

CFG = WatchConfig(poll_s=20, cooldown_s=90, change_threshold=8, daily_cap=40)

# A fixed wall-clock instant (2026-06-14 12:00 local) so day math is deterministic.
NOON = dt.datetime(2026, 6, 14, 12, 0, 0).timestamp()

# Two non-zero hashes that differ in 16 bits — comfortably past the threshold(8), so
# `changed()` is True. Using non-zero values avoids the "no baseline" always-changed path.
HASH_A = 0xFFFF
HASH_B = 0x0000_FFFF_0000  # 16 high bits set, disjoint from HASH_A's low 16


@pytest.fixture(autouse=True)
def _no_real_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real screen: ahash is constant, grab_jpeg returns fake bytes."""

    async def fake_ahash() -> int:
        return 0b1111_0000

    async def fake_grab(*_a: object, **_k: object) -> bytes:
        return b"\xff\xd8\xff-fake-jpeg"

    monkeypatch.setattr(loop.screen, "ahash", fake_ahash)
    monkeypatch.setattr(loop.screen, "grab_jpeg", fake_grab)


# ── decide_action gating (pure, no I/O) ─────────────────────────────────────────

def test_decide_no_change() -> None:
    # cur_hash equals last_hash → unchanged → don't even look, no vision call.
    state = WatchState(last_hash=0b1010, last_suggest_ts=0.0)
    assert decide_action(state, 0b1010, NOON, CFG) == "skip_no_change"


def test_decide_cooldown() -> None:
    # Changed, but only 10s since the last suggestion (< 90s cooldown).
    state = WatchState(last_hash=HASH_A, last_suggest_ts=NOON - 10)
    assert decide_action(state, HASH_B, NOON, CFG) == "skip_cooldown"


def test_decide_cap() -> None:
    state = WatchState(last_hash=HASH_A, last_suggest_ts=NOON - 1000, count_today=40)
    assert decide_action(state, HASH_B, NOON, CFG) == "skip_cap"


def test_decide_look() -> None:
    state = WatchState(last_hash=HASH_A, last_suggest_ts=NOON - 1000, count_today=3)
    assert decide_action(state, HASH_B, NOON, CFG) == "look"


def test_decide_midnight_rollover_resets_count() -> None:
    # Yesterday's state with a full count; "now" is today → count resets to 0,
    # so we're under cap again and free to look.
    yesterday = dt.date(2026, 6, 13)
    state = WatchState(
        last_hash=HASH_A, last_suggest_ts=NOON - 100_000, count_today=40, day=yesterday
    )
    assert decide_action(state, HASH_B, NOON, CFG) == "look"
    assert state.count_today == 0
    assert state.day == dt.date(2026, 6, 14)


# ── full tick (suggest + notify injected) ───────────────────────────────────────

def _suggester(value: str | None):
    """An injectable suggest_fn that ignores its inputs and returns `value`."""
    calls: list[bytes] = []

    async def fn(jpeg: bytes, _model: str) -> str | None:
        calls.append(jpeg)
        return value

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


def _notifier():
    sent: list[tuple[str, str]] = []

    def fn(title: str, body: str) -> None:
        sent.append((title, body))

    fn.sent = sent  # type: ignore[attr-defined]
    return fn


async def test_tick_silent_does_not_notify() -> None:
    state = WatchState(last_hash=0b0000, last_suggest_ts=NOON - 1000, count_today=0)
    notify = _notifier()
    out = await tick(
        state, CFG, NOON, suggest_fn=_suggester(None), notify_fn=notify
    )
    assert out is None
    assert notify.sent == []  # type: ignore[attr-defined]
    assert state.count_today == 0
    assert state.last_suggest_ts == NOON - 1000  # unchanged


async def test_tick_no_change_skips_vision() -> None:
    # last_hash equals what ahash() returns → unchanged → suggest_fn never called.
    state = WatchState(last_hash=0b1111_0000, last_suggest_ts=NOON - 1000)
    sug = _suggester("should not be used")
    notify = _notifier()
    out = await tick(state, CFG, NOON, suggest_fn=sug, notify_fn=notify)
    assert out is None
    assert sug.calls == []  # type: ignore[attr-defined]
    assert notify.sent == []  # type: ignore[attr-defined]


async def test_tick_real_suggestion_notifies_and_updates_state() -> None:
    state = WatchState(last_hash=0b0000, last_suggest_ts=NOON - 1000, count_today=2)
    notify = _notifier()
    msg = "Use ripgrep instead, it's faster."
    out = await tick(state, CFG, NOON, suggest_fn=_suggester(msg), notify_fn=notify)
    assert out == msg
    assert notify.sent == [("Sutra", msg)]  # type: ignore[attr-defined]
    assert state.count_today == 3
    assert state.last_suggest_ts == NOON
    assert state.recent[-1] == msg
    assert state.last_hash == 0b1111_0000  # updated to current capture


async def test_tick_dedupes_same_suggestion() -> None:
    msg = "Pin that dependency version."
    notify = _notifier()
    sug = _suggester(msg)
    state = WatchState(last_hash=0b0000, last_suggest_ts=NOON - 1000, count_today=0)

    first = await tick(state, CFG, NOON, suggest_fn=sug, notify_fn=notify)
    # Second time: changed + past cooldown, same text → deduped (not surfaced).
    state.last_hash = 0b0000  # force "changed" again next tick
    second = await tick(
        state, CFG, NOON + 1000, suggest_fn=sug, notify_fn=notify
    )
    assert first == msg
    assert second is None
    assert len(notify.sent) == 1  # type: ignore[attr-defined]
    assert state.count_today == 1


async def test_tick_speak_invoked_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    spoken: list[str] = []
    monkeypatch.setattr(loop, "_speak", lambda t: spoken.append(t))
    cfg = WatchConfig(cooldown_s=90, daily_cap=40, speak=True)
    state = WatchState(last_hash=0b0000, last_suggest_ts=NOON - 1000)
    await tick(state, cfg, NOON, suggest_fn=_suggester("Run the tests."), notify_fn=_notifier())
    assert spoken == ["Run the tests."]


async def test_tick_suggest_error_never_crashes() -> None:
    async def boom(_jpeg: bytes, _model: str) -> str | None:
        raise RuntimeError("vision down")

    state = WatchState(last_hash=0b0000, last_suggest_ts=NOON - 1000)
    notify = _notifier()
    out = await tick(state, CFG, NOON, suggest_fn=boom, notify_fn=notify)
    assert out is None
    assert notify.sent == []  # type: ignore[attr-defined]
    assert state.last_hash == 0b1111_0000  # still advanced despite the error


# ── suggest_from_screen parsing (litellm mocked, no network) ─────────────────────

def _fake_llm(reply: str):
    async def fake(model: str, messages: list, **_k: object):  # noqa: ANN401
        msg = type("M", (), {"content": reply})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})

    return fake


async def test_suggest_returns_none_on_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(suggest_mod.litellm, "acompletion", _fake_llm("SILENT"))
    assert await suggest_from_screen(b"jpeg") is None


async def test_suggest_returns_clean_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        suggest_mod.litellm, "acompletion", _fake_llm("Use ripgrep instead, it's faster.")
    )
    assert await suggest_from_screen(b"jpeg") == "Use ripgrep instead, it's faster."


def test_clean_suggestion_variants() -> None:
    assert clean_suggestion(None) is None
    assert clean_suggestion("") is None
    assert clean_suggestion("   ") is None
    assert clean_suggestion("SILENT") is None
    assert clean_suggestion("silent") is None
    assert clean_suggestion("...") is None
    # Model prepends SILENT then talks anyway → strip the lead-in.
    assert clean_suggestion("SILENT. Try `git rebase -i`.") == "Try `git rebase -i`."
    # Surrounding quotes/emphasis trimmed.
    assert clean_suggestion('"Close that stray tab."') == "Close that stray tab."
