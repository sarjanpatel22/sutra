"""Tests for desktop capture + change-detection — capture is mocked, nothing is grabbed."""

from __future__ import annotations

import pytest

from friday.vision import screen


def test_hamming() -> None:
    assert screen.hamming(0, 0) == 0
    assert screen.hamming(0b1010, 0b0000) == 2
    assert screen.hamming(0b1111, 0b0000) == 4


def test_changed_no_baseline_or_failed_capture_looks() -> None:
    # First tick (prev=0) and any failed current capture (cur=0) must err toward looking.
    assert screen.changed(0, 12345) is True
    assert screen.changed(12345, 0) is True
    assert screen.changed(0, 0) is True  # both unknown → still look (first-tick safety)


def test_changed_threshold() -> None:
    prev = 0b1               # non-zero baseline so we exercise the threshold, not the guard
    near = prev ^ 0b0111110  # 5 bits different
    far = prev ^ 0b11111110  # 7 bits different
    assert screen.changed(prev, near, threshold=6) is False  # below threshold → quiet
    assert screen.changed(prev, far, threshold=6) is True    # at/above → look


async def test_ahash_averages_pixels(monkeypatch: pytest.MonkeyPatch) -> None:
    # 64 pixels: lower half dark (0), upper half bright (255). avg=127.5 → the bright
    # half sets its bits, the dark half clears them.
    raw = bytes([0] * 32 + [255] * 32)

    async def fake_run(_cmd: list[str]) -> bytes:
        return raw

    monkeypatch.setattr(screen, "_run", fake_run)
    h = await screen.ahash()
    assert h == sum(1 << i for i in range(32, 64))


async def test_ahash_returns_zero_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(_cmd: list[str]) -> bytes:
        raise screen.ScreenUnavailable("no display")

    monkeypatch.setattr(screen, "_run", boom)
    assert await screen.ahash() == 0


async def test_ahash_short_read_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    async def short(_cmd: list[str]) -> bytes:
        return bytes([1, 2, 3])  # fewer than 64 pixels → unusable

    monkeypatch.setattr(screen, "_run", short)
    assert await screen.ahash() == 0


async def test_grab_jpeg_offscreen_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(screen, "screen_available", lambda: False)
    with pytest.raises(screen.ScreenUnavailable):
        await screen.grab_jpeg()
