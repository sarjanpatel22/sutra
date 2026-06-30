"""Tests for `friday.skills.http_methods` — network fully mocked, no real requests."""

from __future__ import annotations

import pytest

from friday.skills import http_methods as hm
from friday.skills.http_methods import check_http_methods


def _responder(monkeypatch, options=None, trace=None):
    """Mock _send to answer per-method. options/trace are (headers, body, status) or None."""
    def fake_send(url, method, headers=None):
        if method == "OPTIONS":
            return options
        if method == "TRACE":
            return trace
        return None
    monkeypatch.setattr(hm, "_send", fake_send)


async def test_reports_dangerous_methods_from_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    _responder(monkeypatch,
               options=({"allow": "GET, POST, PUT, DELETE, OPTIONS"}, "", 200),
               trace=None)
    out = await check_http_methods("http://x.com/")
    assert "PUT" in out["dangerous_methods"] and "DELETE" in out["dangerous_methods"]
    put = next(f for f in out["findings"] if f["id"] == "http-method-put")
    assert put["severity"] == "medium"
    assert "GET" in out["allowed_methods"]  # full set returned


async def test_confirms_trace_xst_by_echoed_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    _responder(monkeypatch,
               options=({"allow": "GET, TRACE"}, "", 200),
               trace=({}, "TRACE / HTTP/1.1\nsutra-trace-probe\n", 200))  # marker echoed
    out = await check_http_methods("http://x.com/")
    assert out["trace_xst"] is True
    assert any(f["id"] == "http-trace-xst" for f in out["findings"])


async def test_trace_not_echoed_is_not_xst(monkeypatch: pytest.MonkeyPatch) -> None:
    _responder(monkeypatch,
               options=({"allow": "GET"}, "", 200),
               trace=({}, "405 Method Not Allowed", 405))  # not honoured
    out = await check_http_methods("http://x.com/")
    assert out["trace_xst"] is False
    assert out["findings"] == []


async def test_clean_endpoint_no_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    _responder(monkeypatch, options=({"allow": "GET, POST, HEAD, OPTIONS"}, "", 200), trace=None)
    out = await check_http_methods("x.com")  # also exercises scheme-prepend
    assert out["dangerous_methods"] == []
    assert out["findings"] == []


async def test_no_options_response_is_graceful(monkeypatch: pytest.MonkeyPatch) -> None:
    _responder(monkeypatch, options=None, trace=None)
    out = await check_http_methods("http://x.com/")
    assert out["allowed_methods"] == []
    assert "none advertised" in out["note"]


async def test_empty_url_errors() -> None:
    assert "error" in await check_http_methods("")


def test_skill_is_auto_confirmed() -> None:
    from friday.skills import registry
    registry.autodiscover()
    assert registry.get("check_http_methods").auto_confirm is True
