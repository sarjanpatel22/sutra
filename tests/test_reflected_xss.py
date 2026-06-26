"""Tests for the reflected-XSS skill (friday/skills/reflected_xss.py) — hermetic local server."""

from __future__ import annotations

import html
import http.server
import threading
from urllib.parse import parse_qs, urlparse

from friday.skills import reflected_xss


def _serve():
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            p = urlparse(self.path)
            q = parse_qs(p.query).get("q", [""])[0]
            if p.path == "/reflect":
                body = f"<html>results for {q}</html>"  # raw reflection (vulnerable)
            elif p.path == "/encoded":
                body = f"<html>results for {html.escape(q)}</html>"  # escaped (safe)
            else:
                body = "<html>nothing</html>"  # no reflection
            self.send_response_only(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def _check(path, **kw):
    srv, port = _serve()
    try:
        return await reflected_xss.test_reflected_xss(f"http://127.0.0.1:{port}{path}", **kw)
    finally:
        srv.shutdown()


async def test_unencoded_reflection_flagged():
    out = await _check("/reflect?q=test")
    assert out["vulnerable"] is True
    assert "q" in out["reflected_unencoded"]
    assert out["findings"][0]["id"] == "reflected-xss"


async def test_encoded_reflection_not_flagged():
    out = await _check("/encoded?q=test")
    assert out["vulnerable"] is False
    assert "q" in out["reflected_encoded"] and out["findings"] == []


async def test_no_reflection_clean():
    out = await _check("/safe?q=test")
    assert out["reflected_unencoded"] == [] and out["reflected_encoded"] == []


async def test_specific_param():
    out = await _check("/reflect?q=test", param="q")
    assert out["tested_params"] == ["q"] and out["vulnerable"] is True


async def test_unreachable_is_graceful():
    out = await reflected_xss.test_reflected_xss("http://127.0.0.1:1/reflect?q=1")
    assert "error" in out


async def test_no_url():
    assert "error" in await reflected_xss.test_reflected_xss("")


def test_test_reflected_xss_is_gated():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("test_reflected_xss").auto_confirm is False  # sends payloads → gated
