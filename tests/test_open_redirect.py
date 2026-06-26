"""Tests for the open-redirect skill (friday/skills/open_redirect.py) — hermetic local server."""

from __future__ import annotations

import http.server
import threading
from urllib.parse import parse_qs, urlparse

from friday.skills import open_redirect


def _serve():
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path == "/redirect" and "next" in qs:  # reflects ?next into Location (vuln)
                self.send_response_only(302)
                self.send_header("Location", qs["next"][0])
            elif parsed.path == "/safe":  # ignores the param (safe)
                self.send_response_only(302)
                self.send_header("Location", "/dashboard")
            else:
                self.send_response_only(200)
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
        return await open_redirect.check_open_redirect(f"http://127.0.0.1:{port}{path}", **kw)
    finally:
        srv.shutdown()


async def test_open_redirect_detected_on_existing_param():
    out = await _check("/redirect?next=/home")
    assert out["vulnerable"] is True
    f = out["findings"][0]
    assert f["id"] == "open-redirect" and "next" in f["name"]
    assert "evil.example" in (out["hits"][0]["location"] or "")


async def test_open_redirect_via_appended_param():
    out = await _check("/redirect")  # no query → skill appends common params
    assert out["vulnerable"] is True
    assert any(h["param"] == "next" for h in out["hits"])


async def test_safe_endpoint_not_flagged():
    out = await _check("/safe?next=/home")  # server ignores next → fixed /dashboard
    assert out["vulnerable"] is False and out["findings"] == []


async def test_specific_param_only():
    out = await _check("/redirect?next=/home", param="next")
    assert out["tested_params"] == ["next"] and out["vulnerable"] is True


async def test_unreachable_is_graceful():
    out = await open_redirect.check_open_redirect("http://127.0.0.1:1/redirect?next=/x")
    assert "error" in out


async def test_no_url():
    assert "error" in await open_redirect.check_open_redirect("")


def test_redirects_to_canary_helper():
    assert open_redirect._redirects_to_canary("https://evil.example/") is True
    assert open_redirect._redirects_to_canary("//evil.example/foo") is True
    assert open_redirect._redirects_to_canary("http://evil.example") is True
    assert open_redirect._redirects_to_canary("/dashboard") is False
    assert open_redirect._redirects_to_canary("https://legit.target.com/home") is False
    assert open_redirect._redirects_to_canary("") is False


def test_check_open_redirect_is_gated():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("check_open_redirect").auto_confirm is False  # active → gated
