"""Tests for the generic http_request skill (mocked transport — no network)."""

from friday.skills import http_request as http_mod
from friday.skills import registry
from friday.skills.http_request import http_request


class _Resp:
    """A minimal streaming httpx.Response stand-in: the body is delivered via aiter_bytes()."""

    def __init__(self, status=200, ctype="application/json", body=b"", encoding="utf-8"):
        self.status_code = status
        self.url = "https://api.example/v1"
        self.headers = {"content-type": ctype}
        self.encoding = encoding
        self._body = body

    async def aiter_bytes(self):
        # hand the body back in a couple of chunks to exercise the accumulation loop
        mid = len(self._body) // 2
        if self._body[:mid]:
            yield self._body[:mid]
        yield self._body[mid:]


class _StreamCM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


def _fake_client(resp, capture=None):
    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, **kw):
            if capture is not None:
                capture.update({"method": method, "url": url, **kw})
            return _StreamCM(resp)

    return _Client


def test_http_request_registered_and_gated():
    registry.autodiscover()
    assert registry.get("http_request").auto_confirm is False  # external I/O → gated


async def test_http_request_parses_json(monkeypatch):
    cap = {}
    monkeypatch.setattr(
        http_mod.httpx, "AsyncClient", _fake_client(_Resp(body=b'{"ok": true}'), cap)
    )
    out = await http_request("https://api.example/v1")
    assert out["status"] == 200 and out["json"] == {"ok": True}
    assert cap["method"] == "GET"


async def test_http_request_text_body_truncated(monkeypatch):
    monkeypatch.setattr(
        http_mod.httpx, "AsyncClient", _fake_client(_Resp(ctype="text/html", body=b"x" * 10000))
    )
    out = await http_request("https://x.test", max_chars=100)
    assert "json" not in out and len(out["body"]) == 100


async def test_http_request_caps_oversized_body(monkeypatch):
    # A body past the byte cap must be bounded (no OOM), flagged truncated, and NOT JSON-parsed.
    monkeypatch.setattr(http_mod, "_MAX_BYTES", 50)
    monkeypatch.setattr(
        http_mod.httpx,
        "AsyncClient",
        _fake_client(_Resp(ctype="application/json", body=b'{"x": "' + b"y" * 500 + b'"}')),
    )
    out = await http_request("https://big.test", max_chars=40)
    assert out["truncated"] is True
    assert "json" not in out and len(out["body"]) == 40


async def test_http_request_post_sends_json_body(monkeypatch):
    cap = {}
    monkeypatch.setattr(
        http_mod.httpx, "AsyncClient", _fake_client(_Resp(body=b'{"id": 1}'), cap)
    )
    await http_request("https://api.example/v1", method="post", json_body={"a": 1})
    assert cap["method"] == "POST" and cap["json"] == {"a": 1}


async def test_http_request_rejects_bad_inputs():
    assert "error" in await http_request("ftp://nope")              # non-http scheme
    assert "error" in await http_request("https://x", method="FROB")  # bogus method
