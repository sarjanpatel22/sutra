"""Tests for the generic http_request skill (mocked transport — no network)."""

from friday.skills import http_request as http_mod
from friday.skills import registry
from friday.skills.http_request import http_request


class _Resp:
    def __init__(self, status=200, ctype="application/json", payload=None, text=""):
        self.status_code = status
        self.url = "https://api.example/v1"
        self.headers = {"content-type": ctype}
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _fake_client(resp, capture=None):
    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kw):
            if capture is not None:
                capture.update({"method": method, "url": url, **kw})
            return resp

    return _Client


def test_http_request_registered_and_gated():
    registry.autodiscover()
    assert registry.get("http_request").auto_confirm is False  # external I/O → gated


async def test_http_request_parses_json(monkeypatch):
    cap = {}
    monkeypatch.setattr(
        http_mod.httpx, "AsyncClient", _fake_client(_Resp(payload={"ok": True}), cap)
    )
    out = await http_request("https://api.example/v1")
    assert out["status"] == 200 and out["json"] == {"ok": True}
    assert cap["method"] == "GET"


async def test_http_request_text_body_truncated(monkeypatch):
    monkeypatch.setattr(
        http_mod.httpx, "AsyncClient", _fake_client(_Resp(ctype="text/html", text="x" * 10000))
    )
    out = await http_request("https://x.test", max_chars=100)
    assert "json" not in out and len(out["body"]) == 100


async def test_http_request_post_sends_json_body(monkeypatch):
    cap = {}
    monkeypatch.setattr(http_mod.httpx, "AsyncClient", _fake_client(_Resp(payload={"id": 1}), cap))
    await http_request("https://api.example/v1", method="post", json_body={"a": 1})
    assert cap["method"] == "POST" and cap["json"] == {"a": 1}


async def test_http_request_rejects_bad_inputs():
    assert "error" in await http_request("ftp://nope")              # non-http scheme
    assert "error" in await http_request("https://x", method="FROB")  # bogus method
