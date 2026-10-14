"""Fleet GPS skill — find where/when a vehicle actually STOPS at a place, from a
telematics breadcrumb log (Geotab / Motive-style CSV: lat, lon, timestamp per ping).

A *real* stop = pings inside a geofence around the site for longer than a dwell
threshold — not a slow drive-by. Typical use: counting how often a truck visits
a customer's yard. Stdlib csv + numpy, no network.
"""

from __future__ import annotations

import asyncio
import csv
import math
from datetime import UTC, datetime
from typing import Any

import numpy as np

from friday.skills.registry import skill

# Candidate column names (lower-cased) for auto-detection across export formats.
_LAT = {"lat", "latitude", "y"}
_LON = {"lon", "lng", "long", "longitude", "x"}
_TIME = {"datetime", "date/time", "timestamp", "time", "date"}
_VEH = {"vehicle", "veh", "unit", "truck", "device", "asset", "name", "id"}
_MAX_STOPS = 2000  # cap the returned list so a pathological call can't flood the model

# strptime fallbacks for non-ISO timestamps (ISO is tried first via fromisoformat).
_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p",
    "%m-%d-%Y %H:%M:%S", "%m-%d-%Y",
)


def _pick(cols: dict[str, str], names: set[str], contains: str = "") -> str | None:
    for k, orig in cols.items():
        if k in names:
            return orig
    if contains:
        for k, orig in cols.items():
            if contains in k:
                return orig
    return None


def _parse_time(s: str) -> datetime | None:
    """Parse a timestamp to a tz-NAIVE datetime (aware inputs are converted to UTC),
    so a file mixing aware and naive stamps still sorts and subtracts cleanly."""
    if not s:
        return None
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in _FORMATS:
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _compute(
    csv_path: str, lat: float, lon: float,
    radius_m: int, min_dwell_min: int, gap_min: int, vehicle: str,
) -> dict[str, Any]:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return {"error": "lat and lon must be numbers"}
    try:
        radius_m = max(0, int(radius_m))
        min_dwell_min = max(0, int(min_dwell_min))
        gap_min = max(0, int(gap_min))
    except (TypeError, ValueError):
        return {"error": "radius_m, min_dwell_min and gap_min must be numbers"}

    veh_l = (vehicle or "").lower().strip()
    times: list[datetime] = []
    lats: list[float] = []
    lons: list[float] = []
    try:
        with open(csv_path, newline="", encoding="utf-8-sig", errors="replace") as f:
            sample = f.read(8192)
            f.seek(0)
            if not sample.strip():
                return {"error": "CSV is empty", "csv_path": csv_path}
            try:
                dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(f, dialect=dialect)
            fields = reader.fieldnames or []
            cols = {str(c).lower().strip(): c for c in fields}
            latc = _pick(cols, _LAT)
            lonc = _pick(cols, _LON)
            timec = _pick(cols, _TIME, contains="time") or _pick(cols, set(), contains="date")
            if not (latc and lonc and timec):
                return {"error": "could not find lat/lon/time columns", "columns": list(fields)}
            veh_cols = [orig for low, orig in cols.items() if low in _VEH]
            for r in reader:  # streamed — never materialises the whole file
                if veh_l and veh_cols and not any(
                    veh_l in str(r.get(c, "")).lower() for c in veh_cols
                ):
                    continue
                t = _parse_time(str(r.get(timec, "")).strip())
                if t is None:
                    continue
                try:
                    la, lo = float(r[latc]), float(r[lonc])
                except (TypeError, ValueError):
                    continue
                times.append(t)
                lats.append(la)
                lons.append(lo)
    except FileNotFoundError:
        return {"error": f"file not found: {csv_path}"}
    except Exception as e:  # noqa: BLE001 — hand the error to the model, don't raise
        return {"error": f"could not read CSV: {e}", "csv_path": csv_path}

    warning = (
        "vehicle filter requested but no vehicle column found; matched all rows"
        if veh_l and not veh_cols else None
    )
    if not times:
        msg = ("no rows matched the vehicle filter" if veh_l and veh_cols
               else "no usable rows after parsing lat/lon/time")
        return {"error": msg, "csv_path": csv_path}

    order = sorted(range(len(times)), key=times.__getitem__)
    times = [times[i] for i in order]
    lat_a = np.array([lats[i] for i in order], dtype=float)
    lon_a = np.array([lons[i] for i in order], dtype=float)

    # Vectorised haversine distance (metres) from every ping to the site.
    R = 6_371_000.0
    p1 = np.radians(lat_a)
    dphi = np.radians(lat - lat_a)
    dl = np.radians(lon - lon_a)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * math.cos(math.radians(lat)) * np.sin(dl / 2) ** 2
    dist = 2 * R * np.arcsin(np.sqrt(a))

    inside = [i for i in range(len(times)) if dist[i] <= radius_m]
    if not inside:
        res = {"site": [lat, lon], "radius_m": radius_m, "stops": [], "visit_count": 0,
               "note": "no breadcrumbs within the geofence"}
        if warning:
            res["warning"] = warning
        return res

    # Walk the in-geofence pings in time order; a gap > gap_min starts a new visit.
    stops: list[dict[str, Any]] = []

    def _close(cl: list[int]) -> None:
        dwell = (times[cl[-1]] - times[cl[0]]).total_seconds() / 60
        if dwell >= min_dwell_min:
            stops.append({
                "arrive": times[cl[0]].isoformat(sep=" "),
                "depart": times[cl[-1]].isoformat(sep=" "),
                "dwell_min": round(dwell, 1),
                "pings": len(cl),
                "closest_m": int(min(dist[j] for j in cl)),
            })

    cl = [inside[0]]
    for prev, cur in zip(inside, inside[1:]):
        if (times[cur] - times[prev]).total_seconds() / 60 > gap_min:
            _close(cl)
            cl = [cur]
        else:
            cl.append(cur)
    _close(cl)

    res: dict[str, Any] = {
        "site": [lat, lon],
        "radius_m": radius_m,
        "min_dwell_min": min_dwell_min,
        "vehicle": vehicle or "all",
        "visit_count": len(stops),
        "total_dwell_min": round(sum(s["dwell_min"] for s in stops), 1),
        "stops": stops[:_MAX_STOPS],
    }
    if len(stops) > _MAX_STOPS:
        res["stops_truncated"] = f"showing first {_MAX_STOPS} of {len(stops)} visits"
    if warning:
        res["warning"] = warning
    return res


@skill(
    name="fleet_gps_stops",
    description=(
        "From a GPS/telematics breadcrumb CSV (columns like lat, lon, datetime), find "
        "every time a vehicle STOPPED at a given coordinate — arrival, departure and dwell "
        "time per visit. A real stop = inside the geofence for more than min_dwell_min "
        "(a brief drive-by is ignored). Use to count a truck's visits to a yard/site, "
        "geofence a location, or tie a vehicle to a pickup/delivery spot. Local file, no key."
    ),
    auto_confirm=True,
    timeout_s=60,
)
async def fleet_gps_stops(
    csv_path: str,
    lat: float,
    lon: float,
    radius_m: int = 250,
    min_dwell_min: int = 10,
    gap_min: int = 30,
    vehicle: str = "",
) -> dict[str, Any]:
    """Detect stops at a coordinate from a breadcrumb CSV.

    csv_path: path to the breadcrumb CSV (auto-detects lat/lon/time columns and delimiter).
    lat, lon: the site coordinate, decimal degrees.
    radius_m: geofence radius; a ping counts as "at the site" within this.
    min_dwell_min: minimum minutes inside the geofence to count as a real stop.
    gap_min: pings more than this many minutes apart start a new visit.
    vehicle: optional — keep only rows whose vehicle/unit column contains this text.
    """
    # Offload the blocking file crunch so it never freezes the assistant's event loop.
    return await asyncio.to_thread(
        _compute, csv_path, lat, lon, radius_m, min_dwell_min, gap_min, vehicle,
    )
