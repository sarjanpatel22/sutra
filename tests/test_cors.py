"""Tests for the CORS misconfig skill (friday/skills/cors.py) — hermetic local server."""

from __future__ import annotations

import http.server
import threading

from friday.skills import cors


def _serve():
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            origin = self.headers.get("Origin")
            self.send_response_only(200)
            if self.path == "/reflect" and origin:  # reflects ANY origin, with credentials
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Credentials", "true")
            elif self.path == "/wildcard":
                self.send_header("Access-Control-Allow-Origin", "*")
            elif self.path == "/null" and origin == "null":
                self.send_header("Access-Control-Allow-Origin", "null")
                self.send_header("Access-Control-Allow-Credentials", "true")
            elif self.path == "/safe":
                self.send_header("Access-Control-Allow-Origin", "https://trusted.example")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def _check(path: str, **kw):
    srv, port = _serve()
    try:
        return await cors.check_cors(f"http://127.0.0.1:{port}{path}", **kw)
    finally:
        srv.shutdown()


def _ids(out):
    return [f["id"] for f in out["findings"]]


async def test_reflects_arbitrary_origin_with_credentials_is_critical():
    out = await _check("/reflect")
    arb = next(f for f in out["findings"] if f["id"] == "cors-reflects-arbitrary-origin")
    assert arb["severity"] == "critical"  # reflected arbitrary origin + credentials
    assert out["vulnerable"] is True
    assert out["findings"][0]["severity"] == "critical"  # worst-first


async def test_null_origin_allowed_flagged():
    out = await _check("/null")
    assert "cors-null-origin" in _ids(out)


async def test_wildcard_is_low():
    out = await _check("/wildcard")
    wc = next(f for f in out["findings"] if f["id"] == "cors-wildcard")
    assert wc["severity"] == "low"


async def test_safe_endpoint_no_findings():
    out = await _check("/safe")
    assert out["findings"] == [] and out["vulnerable"] is False


async def test_custom_origin_reflected_is_bypass():
    out = await _check("/reflect", origins="https://my.bypass.test")
    assert "cors-origin-bypass" in _ids(out)  # non-arbitrary origin reflected
    assert out["tested_origins"] == ["https://my.bypass.test"]


async def test_unreachable_is_graceful():
    out = await cors.check_cors("http://127.0.0.1:1/")
    assert "error" in out


async def test_no_url():
    assert "error" in await cors.check_cors("")


def test_check_cors_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("check_cors").auto_confirm is True
