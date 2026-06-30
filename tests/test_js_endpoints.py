"""Tests for the JS endpoint-extraction skill (friday/skills/js_endpoints.py) — hermetic server.

Serves an HTML page linking a JS bundle; the bundle mixes real endpoints with static-asset noise, so
the regex extraction, noise filtering, and API-endpoint highlighting are all exercised.
"""

from __future__ import annotations

import http.server
import threading

from friday.skills import js_endpoints

_HTML = '<html><head><script src="/static/app.js"></script></head><body>hi</body></html>'
_JS = """
  const API = "/api/v1/users";
  fetch("/rest/admin/config");
  var u = "https://api.example.com/v2/orders?id=1";
  img.src = "/static/logo.png";          // noise — must be dropped
  load("/assets/style.css");             // noise
  const ep = "checkout.json";
  go("/account/settings");
"""


def _serve():
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = _JS if self.path.endswith("app.js") else _HTML
            data = body.encode()
            self.send_response_only(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def _run():
    srv = _serve()
    try:
        return await js_endpoints.extract_js_endpoints(f"http://127.0.0.1:{srv.server_address[1]}/")
    finally:
        srv.shutdown()


async def test_extracts_endpoints_from_js():
    out = await _run()
    assert out["scripts_scanned"] == 1
    eps = out["endpoints"]
    assert "/api/v1/users" in eps
    assert "/rest/admin/config" in eps
    assert "https://api.example.com/v2/orders?id=1" in eps
    assert "/account/settings" in eps


async def test_static_asset_noise_dropped():
    out = await _run()
    joined = " ".join(out["endpoints"])
    assert "logo.png" not in joined and "style.css" not in joined


async def test_api_endpoints_highlighted():
    out = await _run()
    api = out["api_endpoints"]
    assert "/api/v1/users" in api and "/rest/admin/config" in api
    assert "/account/settings" not in api  # not an API-looking path


async def test_unreachable_is_graceful():
    out = await js_endpoints.extract_js_endpoints("http://127.0.0.1:1/")
    assert "error" in out


async def test_no_url():
    assert "error" in await js_endpoints.extract_js_endpoints("")


def test_extract_js_endpoints_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("extract_js_endpoints").auto_confirm is True  # passive GETs → auto
