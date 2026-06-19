"""Tests for the web_search skill (SearXNG) — mocked transport, no network."""

from __future__ import annotations

import httpx

from friday.skills import registry
from friday.skills import web_search as ws_mod
from friday.skills.web_search import web_search


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
    assert registry.get("web_search").auto_confirm is True  # read-only search


async def test_parses_results_and_hits_search_endpoint(monkeypatch):
    payload = {"results": [
        {"title": "T1", "url": "http://a", "content": "snip1"},
        {"title": "T2", "url": "http://b", "content": "snip2"},
    ]}
    cap: dict = {}
    monkeypatch.setattr(ws_mod.httpx, "AsyncClient", _client(_Resp(payload), capture=cap))
    out = await web_search("python")
    assert out == [
        {"title": "T1", "url": "http://a", "snippet": "snip1"},
        {"title": "T2", "url": "http://b", "snippet": "snip2"},
    ]
    assert cap["url"].endswith("/search")
    assert cap["params"]["q"] == "python"


async def test_truncates_to_max_results(monkeypatch):
    payload = {"results": [{"title": f"T{i}", "url": "", "content": ""} for i in range(10)]}
    monkeypatch.setattr(ws_mod.httpx, "AsyncClient", _client(_Resp(payload)))
    assert len(await web_search("q", max_results=3)) == 3


async def test_max_results_clamped_high_and_low(monkeypatch):
    payload = {"results": [{"title": f"T{i}", "url": "", "content": ""} for i in range(30)]}
    monkeypatch.setattr(ws_mod.httpx, "AsyncClient", _client(_Resp(payload)))
    assert len(await web_search("q", max_results=999)) == 20  # clamped to 20
    assert len(await web_search("q", max_results=0)) == 1     # clamped to 1


async def test_missing_item_fields_become_empty_strings(monkeypatch):
    monkeypatch.setattr(ws_mod.httpx, "AsyncClient", _client(_Resp({"results": [{}]})))
    assert await web_search("q") == [{"title": "", "url": "", "snippet": ""}]


async def test_no_results_key_returns_empty_list(monkeypatch):
    monkeypatch.setattr(ws_mod.httpx, "AsyncClient", _client(_Resp({})))
    assert await web_search("q") == []


async def test_network_error_returns_error_dict_not_raise(monkeypatch):
    # SearXNG down (connection refused) → graceful error dict, never a crash.
    monkeypatch.setattr(ws_mod.httpx, "AsyncClient", _client(exc=httpx.ConnectError("refused")))
    out = await web_search("q")
    assert isinstance(out, dict) and "error" in out and out["query"] == "q"


async def test_http_error_returns_error_dict(monkeypatch):
    client = _client(_Resp(raise_exc=RuntimeError("HTTP 500")))
    monkeypatch.setattr(ws_mod.httpx, "AsyncClient", client)
    out = await web_search("q")
    assert isinstance(out, dict) and "error" in out
