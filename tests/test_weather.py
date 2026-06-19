"""Tests for the weather skill (wttr.in) — mocked transport, no network."""

from __future__ import annotations

import httpx

from friday.skills import registry
from friday.skills import weather as weather_mod
from friday.skills.weather import weather


class _Resp:
    def __init__(self, payload=None, raise_exc=None):
        self._payload = payload
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
    assert registry.get("weather").auto_confirm is True


async def test_parses_current_conditions(monkeypatch):
    payload = {
        "current_condition": [{
            "temp_C": "21", "FeelsLikeC": "20", "humidity": "55", "windspeedKmph": "10",
            "weatherDesc": [{"value": "Partly cloudy"}],
        }],
        "nearest_area": [{"areaName": [{"value": "Toronto"}]}],
    }
    cap: dict = {}
    monkeypatch.setattr(weather_mod.httpx, "AsyncClient", _client(_Resp(payload), capture=cap))
    out = await weather("Toronto")
    assert out["location"] == "Toronto"
    assert out["temp_c"] == "21"
    assert out["feels_like_c"] == "20"
    assert out["description"] == "Partly cloudy"
    assert out["humidity_pct"] == "55"
    assert out["wind_kmph"] == "10"
    assert "Toronto" in cap["url"]


async def test_empty_payload_does_not_crash(monkeypatch):
    # wttr returns a sparse/empty body → defensive parsing must yield Nones, not raise.
    monkeypatch.setattr(weather_mod.httpx, "AsyncClient", _client(_Resp({})))
    out = await weather("Nowhere")
    assert out["temp_c"] is None
    assert out["description"] == ""
    assert out["location"] == "Nowhere"  # no area name → falls back to the passed location


async def test_blank_location_resolves_to_current(monkeypatch):
    payload = {"current_condition": [{"temp_C": "5"}], "nearest_area": [{}]}
    monkeypatch.setattr(weather_mod.httpx, "AsyncClient", _client(_Resp(payload)))
    out = await weather("")
    assert out["location"] == "current location"
    assert out["temp_c"] == "5"


async def test_network_error_returns_error_dict(monkeypatch):
    monkeypatch.setattr(weather_mod.httpx, "AsyncClient", _client(exc=httpx.ConnectError("dns")))
    out = await weather("Paris")
    assert "error" in out and out["location"] == "Paris"
