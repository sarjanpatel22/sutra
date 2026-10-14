"""Tests for web_scrape — mocked httpx, no network."""

from __future__ import annotations

import pytest

from friday.skills import registry
from friday.skills import web_scrape as ws_mod
from friday.skills.web_scrape import web_scrape

HTML = """<html><head><title>T</title><style>x{}</style></head><body>
<h1>Hello</h1><p>world</p>
<a href="/a">A</a><a href="https://x.com/b">B</a>
<table><tr><th>Name</th><th>Qty</th></tr><tr><td>Tire</td><td>5</td></tr></table>
<div class="price">$9.99</div>
<script>var z=1;</script></body></html>"""


class _Resp:
    def __init__(self, text="", status=200):
        self.text = text
        self.status_code = status


def _client(resp=None, exc=None):
    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            if exc is not None:
                raise exc
            return resp

    return _Client


def test_registered_and_auto_confirm():
    registry.autodiscover()
    assert registry.get("web_scrape").auto_confirm is True


async def test_text(monkeypatch):
    monkeypatch.setattr(ws_mod.httpx, "AsyncClient", _client(_Resp(HTML)))
    out = await web_scrape("example.com", mode="text")
    assert out["status"] == 200 and out["via"] == "httpx"
    assert "Hello" in out["text"] and "var z" not in out["text"]  # script stripped


async def test_links(monkeypatch):
    monkeypatch.setattr(ws_mod.httpx, "AsyncClient", _client(_Resp(HTML)))
    out = await web_scrape("https://example.com", mode="links")
    assert "https://example.com/a" in out["links"]
    assert "https://x.com/b" in out["links"]


async def test_tables(monkeypatch):
    pytest.importorskip("bs4")  # the tables path needs the optional [scrape] extra
    monkeypatch.setattr(ws_mod.httpx, "AsyncClient", _client(_Resp(HTML)))
    out = await web_scrape("https://example.com", mode="tables")
    assert out["table_count"] == 1
    assert out["tables"][0] == [["Name", "Qty"], ["Tire", "5"]]


async def test_select(monkeypatch):
    pytest.importorskip("bs4")  # the CSS-select path needs the optional [scrape] extra
    monkeypatch.setattr(ws_mod.httpx, "AsyncClient", _client(_Resp(HTML)))
    out = await web_scrape("https://example.com", mode="select", select=".price")
    assert out["count"] == 1 and out["results"][0]["text"] == "$9.99"


async def test_json(monkeypatch):
    monkeypatch.setattr(ws_mod.httpx, "AsyncClient", _client(_Resp('{"ok": true, "n": 3}')))
    out = await web_scrape("https://api.example.com/x", mode="json")
    assert out["data"] == {"ok": True, "n": 3}


async def test_json_invalid(monkeypatch):
    monkeypatch.setattr(ws_mod.httpx, "AsyncClient", _client(_Resp("<html>nope</html>")))
    out = await web_scrape("https://x", mode="json")
    assert "error" in out


async def test_fetch_error(monkeypatch):
    import httpx

    monkeypatch.setattr(ws_mod.httpx, "AsyncClient", _client(exc=httpx.ConnectError("dns")))
    monkeypatch.setattr(ws_mod, "_cloudscraper_get", _fail_cf)
    out = await web_scrape("https://x", mode="text")
    assert "error" in out


async def _fail_cf(url):
    return None, None, "cloudscraper failed"


async def test_empty_url():
    out = await web_scrape("")
    assert "error" in out


async def test_tables_without_bs4(monkeypatch):
    monkeypatch.setattr(ws_mod, "_HAS_BS4", False)
    out = await web_scrape("https://x", mode="tables")
    assert "error" in out and "scrape" in out["error"]


async def test_block_hint_points_to_browser_agent(monkeypatch):
    # httpx 403 -> escalates to cloudscraper, which also hits a JS wall (403 body).
    monkeypatch.setattr(ws_mod.httpx, "AsyncClient", _client(_Resp("blocked", status=403)))

    async def _cf(url):
        return "<html>Security Check — please enable JavaScript</html>", 403, None

    monkeypatch.setattr(ws_mod, "_cloudscraper_get", _cf)
    out = await web_scrape("https://www.g2.com", mode="text")
    assert out["status"] == 403 and out["via"] == "cloudscraper"
    assert out["blocked"] is True and "browser_agent" in out["hint"]
