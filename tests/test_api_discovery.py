"""Tests for the API-discovery skill (friday/skills/api_discovery.py) — hermetic server.

Serves an OpenAPI spec at /openapi.json and a live GraphQL endpoint at /graphql; everything else
404s. Exercises spec detection + route extraction + GraphQL confirmation, no network.
"""

from __future__ import annotations

import http.server
import json
import threading

from friday.skills import api_discovery

_SPEC = json.dumps({
    "openapi": "3.0.0",
    "paths": {
        "/users/{id}": {"get": {}, "delete": {}},
        "/orders": {"get": {}, "post": {}},
        "/admin/config": {"put": {}},
    },
})


def _serve():
    class _H(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body):
            data = body.encode()
            self.send_response_only(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if self.path == "/openapi.json":
                self._send(200, _SPEC)
            else:
                self._send(404, "not found")

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            if self.path == "/graphql":
                self._send(200, '{"data":{"__typename":"Query"}}')
            else:
                self._send(404, "not found")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def _run():
    srv = _serve()
    try:
        return await api_discovery.discover_api(f"http://127.0.0.1:{srv.server_address[1]}/")
    finally:
        srv.shutdown()


async def test_finds_spec_and_extracts_routes():
    out = await _run()
    assert len(out["specs"]) == 1 and out["specs"][0]["type"] == "openapi/swagger"
    eps = out["endpoints"]
    assert "GET /users/{id}" in eps
    assert "DELETE /users/{id}" in eps
    assert "POST /orders" in eps
    assert "PUT /admin/config" in eps


async def test_detects_graphql_endpoint():
    out = await _run()
    assert any(g.endswith("/graphql") for g in out["graphql"])
    assert any(f["id"] == "graphql-endpoint" for f in out["findings"])


async def test_exposed_spec_is_info_finding():
    out = await _run()
    spec_f = next(f for f in out["findings"] if f["id"] == "exposed-api-spec")
    assert spec_f["severity"] == "info" and "routes" in spec_f["name"]


def test_parse_openapi_ignores_non_methods():
    spec = json.dumps({"paths": {"/x": {"get": {}, "parameters": [], "summary": "y"}}})
    routes = api_discovery._parse_openapi(spec)
    assert routes == ["GET /x"]  # 'parameters'/'summary' are not HTTP methods


async def test_versioned_swagger_path_is_probed():
    # regression: petstore-style spec served at /v2/swagger.json must be discovered
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = _SPEC if self.path == "/v2/swagger.json" else "not found"
            code = 200 if self.path == "/v2/swagger.json" else 404
            data = body.encode()
            self.send_response_only(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            self.send_response_only(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = await api_discovery.discover_api(f"http://127.0.0.1:{srv.server_address[1]}/")
    finally:
        srv.shutdown()
    assert len(out["specs"]) == 1 and "GET /orders" in out["endpoints"]


async def test_unreachable_is_graceful():
    out = await api_discovery.discover_api("http://127.0.0.1:1/")
    assert "error" in out


async def test_no_url():
    assert "error" in await api_discovery.discover_api("")


def test_discover_api_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("discover_api").auto_confirm is True  # fixed GETs + benign query → passive
