"""Tests for the SSTI skill (friday/skills/ssti.py) - hermetic local server that 'evaluates'."""

from __future__ import annotations

import http.server
import threading
from urllib.parse import parse_qs, urlparse

from friday.skills import ssti


def _serve():
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            p = urlparse(self.path)
            v = parse_qs(p.query).get("name", [""])[0]
            if p.path == "/ssti" and "1337*1337" in v:   # simulate a template engine evaluating it
                body = "Hello 1787569 welcome"
            else:                                         # /safe etc - reflect raw, no evaluation
                body = f"Hello {v}"
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
        return await ssti.probe_ssti(f"http://127.0.0.1:{port}{path}", **kw)
    finally:
        srv.shutdown()


async def test_ssti_detected_when_template_evaluates():
    out = await _check("/ssti?name=test")
    assert out["vulnerable"] is True
    f = out["findings"][0]
    assert f["id"] == "ssti" and f["severity"] == "high" and "name" in f["name"]


async def test_no_ssti_when_input_reflected_raw():
    out = await _check("/safe?name=test")
    assert out["vulnerable"] is False and out["findings"] == []


async def test_specific_param():
    out = await _check("/ssti?name=test", param="name")
    assert out["tested_params"] == ["name"] and out["vulnerable"] is True


async def test_unreachable_is_graceful():
    out = await ssti.probe_ssti("http://127.0.0.1:1/ssti?name=1")
    assert "error" in out


async def test_no_url():
    assert "error" in await ssti.probe_ssti("")


def test_probe_ssti_is_gated():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("probe_ssti").auto_confirm is False  # sends payloads -> gated
