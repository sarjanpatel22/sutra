"""Tests for the geo skills (Nominatim + Esri) — mocked transport, no network."""

from __future__ import annotations

import httpx

from friday.skills import geo as geo_mod
from friday.skills import registry
from friday.skills.geo import geocode, reverse_geocode, satellite_view


class _Resp:
    def __init__(self, payload=None, content=b"", raise_exc=None):
        self._payload = payload
        self.content = content
        self._raise = raise_exc

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def json(self):
        return self._payload


def _client(resp=None, exc=None, capture=None):
    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            if capture is not None:
                capture.update({"url": url, **kw})
            if exc is not None:
                raise exc
            return resp

    return _Client


def test_registered_and_auto_confirm():
    registry.autodiscover()
    for name in ("geocode", "reverse_geocode", "satellite_view"):
        assert registry.get(name).auto_confirm is True


async def test_geocode_parses(monkeypatch):
    payload = [
        {
            "display_name": "Science North, Sudbury, ON",
            "lat": "46.5366",
            "lon": "-80.9036",
            "type": "industrial",
            "class": "landuse",
            "boundingbox": ["46.535", "46.538", "-80.905", "-80.902"],
        }
    ]
    cap: dict = {}
    monkeypatch.setattr(geo_mod.httpx, "AsyncClient", _client(_Resp(payload), capture=cap))
    out = await geocode("Science North, Sudbury")
    assert out["found"] is True
    assert out["lat"] == 46.5366 and out["lon"] == -80.9036
    assert out["bbox_south_north_west_east"] == [46.535, 46.538, -80.905, -80.902]
    assert len(out["candidates"]) == 1
    assert cap["params"]["q"] == "Science North, Sudbury"


async def test_geocode_empty_place_no_network(monkeypatch):
    # must short-circuit before any HTTP call
    monkeypatch.setattr(
        geo_mod.httpx, "AsyncClient", _client(exc=AssertionError("should not be called"))
    )
    out = await geocode("   ")
    assert out["found"] is False and "error" in out


async def test_geocode_no_results(monkeypatch):
    monkeypatch.setattr(geo_mod.httpx, "AsyncClient", _client(_Resp([])))
    out = await geocode("Nowheresville XYZ")
    assert out["found"] is False


async def test_geocode_network_error(monkeypatch):
    monkeypatch.setattr(
        geo_mod.httpx, "AsyncClient", _client(exc=httpx.ConnectError("dns"))
    )
    out = await geocode("Toronto")
    assert "error" in out and out["found"] is False


async def test_reverse_geocode_parses(monkeypatch):
    payload = {
        "display_name": "2870 Ramsey Lake Rd, Greater Sudbury, ON",
        "address": {"road": "Ramsey Lake Rd", "city": "Greater Sudbury"},
    }
    monkeypatch.setattr(geo_mod.httpx, "AsyncClient", _client(_Resp(payload)))
    out = await reverse_geocode(46.4948, -80.9015)
    assert out["found"] is True
    assert "Ramsey Lake Rd" in out["address"]
    assert out["details"]["city"] == "Greater Sudbury"


async def test_reverse_geocode_error(monkeypatch):
    monkeypatch.setattr(
        geo_mod.httpx, "AsyncClient", _client(exc=httpx.ConnectError("x"))
    )
    out = await reverse_geocode(0.0, 0.0)
    assert "error" in out


async def test_satellite_view_saves_png(monkeypatch, tmp_path):
    import os
    from pathlib import Path

    monkeypatch.setattr(geo_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        geo_mod.httpx,
        "AsyncClient",
        _client(_Resp(content=b"\x89PNG\r\n\x1a\n-fake-aerial-bytes")),
    )
    out = await satellite_view(46.5366, -80.9036, radius_m=300)
    p = Path(out["image_path"])
    assert p.exists() and p.read_bytes().startswith(b"\x89PNG")
    assert out["point_pixel"] == [500, 500]
    # bbox west < east, south < north
    w, s, e, n = out["bbox_west_south_east_north"]
    assert w < e and s < n
    os.remove(p)


async def test_satellite_view_network_error(monkeypatch):
    monkeypatch.setattr(
        geo_mod.httpx, "AsyncClient", _client(exc=httpx.ConnectError("x"))
    )
    out = await satellite_view(46.5, -80.9)
    assert "error" in out
