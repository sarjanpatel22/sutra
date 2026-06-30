"""Tests for the parameter-discovery skill (friday/skills/param_discovery.py) — hermetic server.

The server reflects the values of a chosen set of parameters (and ignores the rest), so the skill
must correctly identify exactly the reflected ones via their unique canaries.
"""

from __future__ import annotations

import http.server
import threading
from urllib.parse import parse_qs, urlparse

from friday.skills import param_discovery


def _serve(reflect: set[str]):
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            shown = [q[k][0] for k in reflect if k in q]  # only these params are reflected
            body = "<html>" + " ".join(shown) + "</html>"
            data = body.encode()
            self.send_response_only(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def _find(reflect: set[str], **kw):
    srv, port = _serve(reflect)
    try:
        return await param_discovery.find_params(f"http://127.0.0.1:{port}/", **kw)
    finally:
        srv.shutdown()


async def test_discovers_reflected_params():
    out = await _find({"redirect", "debug", "callback"})
    assert set(out["discovered"]) == {"redirect", "debug", "callback"}
    assert out["findings"][0]["id"] == "reflected-parameter"


async def test_ignores_unreflected_params():
    out = await _find({"id"})  # only 'id' reflects; dozens of others must not be reported
    assert out["discovered"] == ["id"]


async def test_echo_all_app_not_reported():
    # app that reflects EVERY param (echoes the query) must not yield false positives
    class _Echo(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            body = "<html>" + " ".join(v[0] for v in q.values()) + "</html>"
            data = body.encode()
            self.send_response_only(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = await param_discovery.find_params(f"http://127.0.0.1:{srv.server_address[1]}/")
    finally:
        srv.shutdown()
    assert out["discovered"] == []  # no false positives
    assert "arbitrary parameters" in out["note"]


async def test_no_params_reflected():
    out = await _find(set())
    assert out["discovered"] == [] and out["findings"] == []


async def test_extra_wordlist_param_found():
    out = await _find({"zzcustom"}, extra="zzcustom, another")
    assert "zzcustom" in out["discovered"]
    assert out["tested"] > len(param_discovery._WORDLIST)  # extras were added


async def test_unreachable_is_graceful():
    out = await param_discovery.find_params("http://127.0.0.1:1/")
    assert "error" in out


async def test_no_url():
    assert "error" in await param_discovery.find_params("")


def test_find_params_is_gated():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("find_params").auto_confirm is False  # active probes → gated
