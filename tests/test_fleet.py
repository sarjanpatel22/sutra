"""Tests for the fleet_gps_stops skill — synthetic breadcrumb CSVs, no network."""

from __future__ import annotations

from pathlib import Path

from friday.skills import registry
from friday.skills.fleet import fleet_gps_stops

# A site and a few pings around it. ~100 m north of the site is well inside 250 m;
# the "far" point is several km away (outside any sane geofence).
SITE = (46.5366, -80.9036)
NEAR = (46.5375, -80.9036)  # ~100 m north
FAR = (46.6000, -80.8000)


def _write(tmp_path: Path, rows: list[tuple[str, float, float, str]]) -> str:
    """rows = (datetime, lat, lon, vehicle); returns the CSV path."""
    lines = ["DateTime,Lat,Lon,Vehicle"]
    for t, la, lo, v in rows:
        lines.append(f"{t},{la},{lo},{v}")
    p = tmp_path / "breadcrumbs.csv"
    p.write_text("\n".join(lines))
    return str(p)


def test_registered_and_auto_confirm():
    registry.autodiscover()
    assert registry.get("fleet_gps_stops").auto_confirm is True


async def test_detects_a_real_stop(tmp_path):
    rows = [
        ("2026-01-15 08:00:00", *FAR, "Truck 01"),   # driving in
        ("2026-01-15 09:00:00", *NEAR, "Truck 01"),  # arrive
        ("2026-01-15 09:05:00", *SITE, "Truck 01"),
        ("2026-01-15 09:10:00", *NEAR, "Truck 01"),
        ("2026-01-15 09:20:00", *SITE, "Truck 01"),  # depart (20 min dwell)
        ("2026-01-15 10:00:00", *FAR, "Truck 01"),   # driving out
    ]
    out = await fleet_gps_stops(_write(tmp_path, rows), *SITE)
    assert out["visit_count"] == 1
    stop = out["stops"][0]
    assert stop["dwell_min"] == 20.0
    assert stop["pings"] == 4
    assert stop["closest_m"] == 0
    assert out["total_dwell_min"] == 20.0


async def test_ignores_driveby(tmp_path):
    # Inside the geofence, but only 3 minutes — below the 10-min dwell floor.
    rows = [
        ("2026-01-15 09:00:00", *NEAR, "Truck 01"),
        ("2026-01-15 09:01:30", *SITE, "Truck 01"),
        ("2026-01-15 09:03:00", *NEAR, "Truck 01"),
    ]
    out = await fleet_gps_stops(_write(tmp_path, rows), *SITE)
    assert out["visit_count"] == 0
    assert out["stops"] == []


async def test_two_visits_split_by_gap(tmp_path):
    rows = [
        ("2026-01-15 09:00:00", *SITE, "Truck 01"),
        ("2026-01-15 09:15:00", *SITE, "Truck 01"),  # visit A: 15 min
        ("2026-01-15 13:00:00", *SITE, "Truck 01"),  # ~4 h gap -> new visit
        ("2026-01-15 13:20:00", *SITE, "Truck 01"),  # visit B: 20 min
    ]
    out = await fleet_gps_stops(_write(tmp_path, rows), *SITE)
    assert out["visit_count"] == 2
    assert [s["dwell_min"] for s in out["stops"]] == [15.0, 20.0]


async def test_vehicle_filter(tmp_path):
    rows = [
        ("2026-01-15 09:00:00", *SITE, "Truck 01"),
        ("2026-01-15 09:15:00", *SITE, "Truck 01"),
        ("2026-01-16 09:00:00", *SITE, "Truck 02"),
        ("2026-01-16 09:15:00", *SITE, "Truck 02"),
    ]
    csv = _write(tmp_path, rows)
    both = await fleet_gps_stops(csv, *SITE)
    assert both["visit_count"] == 2
    one = await fleet_gps_stops(csv, *SITE, vehicle="01")
    assert one["visit_count"] == 1
    assert one["vehicle"] == "01"
    assert one["stops"][0]["arrive"].startswith("2026-01-15")


async def test_no_pings_in_geofence(tmp_path):
    rows = [
        ("2026-01-15 09:00:00", *FAR, "Truck 01"),
        ("2026-01-15 09:15:00", *FAR, "Truck 01"),
    ]
    out = await fleet_gps_stops(_write(tmp_path, rows), *SITE)
    assert out["visit_count"] == 0
    assert out["stops"] == []
    assert "note" in out


async def test_missing_columns(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("foo,bar\n1,2\n")
    out = await fleet_gps_stops(str(p), *SITE)
    assert "error" in out
    assert out["columns"] == ["foo", "bar"]


async def test_bad_path():
    out = await fleet_gps_stops("/no/such/file.csv", *SITE)
    assert "error" in out


async def test_empty_csv(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("DateTime,Lat,Lon,Vehicle\n")
    out = await fleet_gps_stops(str(p), *SITE)
    assert "error" in out
