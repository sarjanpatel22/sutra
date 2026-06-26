"""Tests for the host-header injection skill (friday/skills/host_header.py) — hermetic server."""

from __future__ import annotations

import http.server
import threading

from friday.skills import host_header


def _serve():
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            host = self.headers.get("Host", "")
            xfh = self.headers.get("X-Forwarded-Host", "")
            body = ""
            if self.path == "/reflect_body":  # builds a reset link from Host
                body = f'<html><a href="https://{host}/reset?token=abc">reset</a></html>'
                self.send_response_only(200)
            elif self.path == "/redirect":  # redirects using Host
                self.send_response_only(302)
                self.send_header("Location", f"https://{host}/")
            elif self.path == "/xfh":  # builds canonical from XFH
                body = f'<link rel="canonical" href="https://{xfh}/">'
                self.send_response_only(200)
            else:  # /safe — ignores the headers
                body = "<html>nothing reflected here</html>"
                self.send_response_only(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def _check(path: str):
    srv, port = _serve()
    try:
        return await host_header.check_host_header(f"http://127.0.0.1:{port}{path}")
    finally:
        srv.shutdown()


def _ids(out):
    return [f["id"] for f in out["findings"]]


async def test_host_reflected_in_body_is_medium():
    out = await _check("/reflect_body")
    assert out["vulnerable"] is True
    f = next(f for f in out["findings"] if f["id"] == "host-header-reflected")
    assert f["severity"] == "medium" and "Host" in f["name"]


async def test_host_reflected_in_redirect_is_high():
    out = await _check("/redirect")
    f = next(f for f in out["findings"] if f["id"] == "host-header-redirect")
    assert f["severity"] == "high"
    assert out["findings"][0]["severity"] == "high"  # worst-first


async def test_x_forwarded_host_reflected():
    out = await _check("/xfh")
    f = next(f for f in out["findings"] if f["id"] == "host-header-reflected")
    assert "X-Forwarded-Host" in f["name"]  # the XFH vector, not Host


async def test_safe_endpoint_no_findings():
    out = await _check("/safe")
    assert out["vulnerable"] is False and out["findings"] == []


async def test_unreachable_is_graceful():
    out = await host_header.check_host_header("http://127.0.0.1:1/")
    assert "error" in out


async def test_no_url():
    assert "error" in await host_header.check_host_header("")


def test_check_host_header_is_gated():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("check_host_header").auto_confirm is False  # active → gated
