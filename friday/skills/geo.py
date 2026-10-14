"""Geo skills — address <-> GPS coordinates (OpenStreetMap / Nominatim) and
satellite-photo verification (Esri World Imagery).

The pattern these support: a mailing address or a reverse-geocoded label is often
NOT where activity actually happens (a yard sits a few hundred metres from the gate,
a name matches two businesses). So: get authoritative coordinates, then *look at the
aerial* to confirm the real site. No API keys — both services are public.
"""

from __future__ import annotations

import asyncio
import math
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from friday.skills.registry import skill

_NOMINATIM = "https://nominatim.openstreetmap.org"
_ESRI = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/export"
)
# Nominatim asks for a real User-Agent and no more than ~1 request/second.
_UA = {"User-Agent": "sutra-geo/1.0 (personal assistant; +local use)"}
_MIN_INTERVAL = 1.0  # seconds between Nominatim calls (their usage policy)
_nominatim_lock = asyncio.Lock()
_last_nominatim = 0.0


async def _throttle() -> None:
    """Space Nominatim calls to ~1/sec so a loop of lookups isn't rate-limited."""
    global _last_nominatim
    async with _nominatim_lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_nominatim)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_nominatim = time.monotonic()


def _bbox(x: dict) -> list[float] | None:
    bb = x.get("boundingbox") or []
    try:
        return [float(v) for v in bb] if len(bb) == 4 else None
    except (TypeError, ValueError):
        return None


@skill(
    name="geocode",
    description=(
        "Turn a place name or street address into GPS coordinates (forward geocode) — "
        "e.g. 'Science North, Sudbury, Ontario' or '2870 Ramsey Lake Rd, Sudbury'. Returns "
        "lat/lon plus the property's bounding box and best candidates. Use this to put a "
        "business/site on the map. Public OpenStreetMap data, no key. For 'what's AT a "
        "coordinate' use reverse_geocode; to SEE the site use satellite_view."
    ),
    auto_confirm=True,
    timeout_s=20,
)
async def geocode(place: str, limit: int = 1) -> dict[str, Any]:
    """Forward-geocode a name/address to coordinates.

    place: a name or address, e.g. "CARSTAR, Sudbury, Ontario".
    limit: how many candidates to return (1-5); the best is also at the top level.
    """
    place = (place or "").strip()
    if not place:
        return {"error": "empty place", "found": False}
    try:
        limit = max(1, min(5, int(limit)))
    except (TypeError, ValueError):
        limit = 1
    try:
        await _throttle()
        async with httpx.AsyncClient(timeout=15, headers=_UA) as client:
            r = await client.get(
                f"{_NOMINATIM}/search",
                params={"q": place, "format": "json", "limit": limit},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001 — hand the error to the model, don't raise
        return {"error": f"geocode failed: {e}", "place": place, "found": False}

    if not isinstance(data, list) or not data:
        return {"place": place, "found": False}

    def _one(x: dict) -> dict | None:
        try:
            lat_v, lon_v = float(x["lat"]), float(x["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        return {
            "display_name": x.get("display_name"),
            "lat": lat_v,
            "lon": lon_v,
            "type": x.get("type"),
            "category": x.get("class"),
            "bbox_south_north_west_east": _bbox(x),
        }

    cands = [c for c in (_one(x) for x in data) if c]
    if not cands:
        return {"place": place, "found": False}
    top = cands[0]
    return {
        "place": place,
        "found": True,
        "lat": top["lat"],
        "lon": top["lon"],
        "display_name": top["display_name"],
        "type": top["type"],
        "bbox_south_north_west_east": top["bbox_south_north_west_east"],
        "candidates": cands,
        "note": (
            "geocoding is a lead, not proof — confirm with satellite_view "
            "when the exact spot matters"
        ),
    }


@skill(
    name="reverse_geocode",
    description=(
        "Turn GPS coordinates into the nearest address / what OpenStreetMap thinks is at "
        "that spot. The inverse of geocode. Public OSM data, no key."
    ),
    auto_confirm=True,
    timeout_s=20,
)
async def reverse_geocode(lat: float, lon: float) -> dict[str, Any]:
    """Reverse-geocode a coordinate to an address.

    lat, lon: decimal degrees (e.g. 46.5366, -80.9036).
    """
    try:
        await _throttle()
        async with httpx.AsyncClient(timeout=15, headers=_UA) as client:
            r = await client.get(
                f"{_NOMINATIM}/reverse",
                params={"lat": lat, "lon": lon, "format": "json"},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        return {"error": f"reverse geocode failed: {e}", "lat": lat, "lon": lon}

    if not isinstance(data, dict) or data.get("error"):
        return {"lat": lat, "lon": lon, "address": None, "found": False}
    return {
        "lat": lat,
        "lon": lon,
        "address": data.get("display_name"),
        "details": data.get("address", {}),
        "found": True,
    }


@skill(
    name="satellite_view",
    description=(
        "Download the satellite / aerial photo of a GPS point (Esri World Imagery) and "
        "save it as a PNG so you can SEE what is physically there — confirm a loading yard "
        "vs a mailing address, a quarry, a dealership lot, a landfill, etc. The point sits "
        "at the CENTRE of the frame. Returns the image path; open/view it to verify. No "
        "key. Pair with geocode."
    ),
    auto_confirm=True,
    timeout_s=25,
)
async def satellite_view(lat: float, lon: float, radius_m: int = 400) -> dict[str, Any]:
    """Fetch an aerial photo centred on a coordinate.

    lat, lon: centre of the view, decimal degrees.
    radius_m: half-width of the frame in metres (~200-1000 reads well for a worksite).
    """
    try:
        lat, lon = float(lat), float(lon)
        radius_m = max(50, min(5000, int(radius_m)))
    except (TypeError, ValueError):
        return {"error": "lat, lon and radius_m must be numbers"}
    # Equirectangular bbox around the point; pixel mapping is then linear.
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    west, south, east, north = lon - dlon, lat - dlat, lon + dlon, lat + dlat
    try:
        async with httpx.AsyncClient(timeout=25, headers=_UA) as client:
            r = await client.get(
                _ESRI,
                params={
                    "bbox": f"{west},{south},{east},{north}",
                    "bboxSR": 4326,
                    "imageSR": 4326,
                    "size": "1000,1000",
                    "format": "png",
                    "f": "image",
                },
            )
            r.raise_for_status()
            img_bytes = r.content
    except Exception as e:  # noqa: BLE001
        return {"error": f"satellite fetch failed: {e}", "lat": lat, "lon": lon}

    if not img_bytes:
        return {"error": "empty image from imagery service", "lat": lat, "lon": lon}
    if not img_bytes.startswith(b"\x89PNG"):
        # Esri returns a JSON error with HTTP 200 on a bad request — don't save it as a PNG.
        return {
            "error": "imagery service did not return a PNG (likely a service error)",
            "detail": img_bytes[:200].decode("utf-8", "replace"),
            "lat": lat,
            "lon": lon,
        }

    out = Path(tempfile.gettempdir()) / f"sat_{time.time_ns()}.png"
    out.write_bytes(img_bytes)
    return {
        "lat": lat,
        "lon": lon,
        "radius_m": radius_m,
        "image_path": str(out),
        "image_size": [1000, 1000],
        "point_pixel": [500, 500],  # the queried point is the centre of the frame
        "bbox_west_south_east_north": [west, south, east, north],
        "hint": (
            "open image_path to see the site — your point is the CENTRE of the image"
        ),
    }
