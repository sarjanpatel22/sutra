"""Weather skill via wttr.in — no API key required."""

from __future__ import annotations

from typing import Any

import httpx

from friday.skills.registry import skill


@skill(
    name="weather",
    description="Get the current weather for a city (blank = your IP-based location).",
    auto_confirm=True,
    timeout_s=10,
)
async def weather(location: str = "") -> dict[str, Any]:
    """Current weather conditions.

    location: City name, e.g. "Toronto". Blank uses IP-based location.
    """
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            # wttr.in returns HTML to browsers; a curl-like UA + j1 gives JSON.
            r = await client.get(
                f"https://wttr.in/{location.strip()}",
                params={"format": "j1"},
                headers={"User-Agent": "curl/8"},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001 — return the error to the model, don't raise
        return {"error": f"weather lookup failed: {e}", "location": location}

    cur = (data.get("current_condition") or [{}])[0]
    areas = data.get("nearest_area") or [{}]
    city = ""
    if areas and areas[0].get("areaName"):
        city = areas[0]["areaName"][0].get("value", "")
    return {
        "location": city or location or "current location",
        "temp_c": cur.get("temp_C"),
        "feels_like_c": cur.get("FeelsLikeC"),
        "description": (cur.get("weatherDesc") or [{}])[0].get("value", ""),
        "humidity_pct": cur.get("humidity"),
        "wind_kmph": cur.get("windspeedKmph"),
    }
