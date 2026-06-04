"""Date/time skill — the current time, in any IANA timezone."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from friday.skills.registry import skill


@skill(
    name="current_time",
    description="Get the current date and time, optionally for a given timezone.",
    auto_confirm=True,
    timeout_s=2,
)
async def current_time(timezone: str = "America/Toronto") -> dict[str, Any]:
    """Current date and time.

    timezone: IANA timezone name, e.g. "America/Toronto", "UTC", "Asia/Kolkata".
    """
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return {"error": f"unknown timezone: {timezone}"}
    now = datetime.now(tz)
    return {
        "iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": now.strftime("%A"),
        "timezone": timezone,
    }
