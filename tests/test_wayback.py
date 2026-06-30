"""Tests for the wayback skill (friday/skills/wayback.py) — hermetic CDX server.

A local server stands in for the Wayback CDX API (the skill's _CDX base is pointed at it), returning
CDX-style JSON so parsing, dedupe, and the params_only filter are exercised with no network.
"""

from __future__ import annotations

import http.server
import json
import threading

from friday.skills import wayback

_CDX_JSON = [
    ["original"],  # header row — must be skipped
    ["http://x.com/"],
    ["http://x.com/item?id=1"],
    ["http://x.com/item?id=1"],  # duplicate — must be deduped
    ["http://api.x.com/search?q=test"],
    ["http://x.com/about"],
]


def _serve():
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            data = json.dumps(_CDX_JSON).encode()
            self.send_response_only(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def _run(monkeypatch, **kw):
    srv = _serve()
    monkeypatch.setattr(wayback, "_CDX", f"http://127.0.0.1:{srv.server_address[1]}/cdx")
    try:
        return await wayback.wayback_urls("x.com", **kw)
    finally:
        srv.shutdown()


async def test_harvests_and_dedupes(monkeypatch):
    out = await _run(monkeypatch)
    assert out["total"] == 4  # 5 unique rows minus the 1 duplicate
    assert "http://x.com/item?id=1" in out["urls"]
    assert out["with_params"] == 2  # item?id and search?q


async def test_params_only_filter(monkeypatch):
    out = await _run(monkeypatch, params_only=True)
    assert out["total"] == 2
    assert all("?" in u and "=" in u for u in out["urls"])


async def test_bare_domain_normalization():
    assert wayback._bare("https://*.Sub.X.COM:8443/path") == "sub.x.com"


async def test_unreachable_is_graceful(monkeypatch):
    monkeypatch.setattr(wayback, "_CDX", "http://127.0.0.1:1/cdx")
    out = await wayback.wayback_urls("x.com")
    assert "error" in out


async def test_no_domain():
    assert "error" in await wayback.wayback_urls("")


def test_wayback_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("wayback_urls").auto_confirm is True  # queries archive.org, not the target
