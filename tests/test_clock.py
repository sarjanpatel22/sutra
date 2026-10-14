"""Tests for the current_time skill (friday/skills/clock.py)."""

from __future__ import annotations

import datetime as dt

from friday.skills.clock import current_time


async def test_returns_all_fields_for_default_tz() -> None:
    res = await current_time()
    assert set(res) == {"iso", "date", "time", "weekday", "timezone"}
    assert res["timezone"] == "America/Toronto"
    # date/time are parseable in the documented formats
    dt.datetime.strptime(res["date"], "%Y-%m-%d")
    dt.datetime.strptime(res["time"], "%H:%M:%S")
    assert res["weekday"] in {
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    }


async def test_utc_offset_is_zero() -> None:
    res = await current_time("UTC")
    assert res["timezone"] == "UTC"
    # a UTC isoformat carries a +00:00 offset
    parsed = dt.datetime.fromisoformat(res["iso"])
    assert parsed.utcoffset() == dt.timedelta(0)


async def test_distinct_zones_differ() -> None:
    # Toronto and Kolkata are never in the same offset — the clock reflects the tz.
    tor = await current_time("America/Toronto")
    kol = await current_time("Asia/Kolkata")
    assert dt.datetime.fromisoformat(tor["iso"]).utcoffset() != \
        dt.datetime.fromisoformat(kol["iso"]).utcoffset()


async def test_unknown_timezone_returns_error() -> None:
    res = await current_time("Mars/Olympus_Mons")
    assert res == {"error": "unknown timezone: Mars/Olympus_Mons"}
