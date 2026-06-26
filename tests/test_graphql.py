"""Tests for the GraphQL introspection skill (friday/skills/graphql.py) — hermetic local server."""

from __future__ import annotations

import http.server
import json
import threading

from friday.skills import graphql

_SCHEMA = {
    "data": {
        "__schema": {
            "queryType": {"name": "Query"},
            "mutationType": {"name": "Mutation"},
            "types": [
                {
                    "name": "Query",
                    "kind": "OBJECT",
                    "fields": [{"name": "me"}, {"name": "users"}, {"name": "product"}],
                },
                {
                    "name": "Mutation",
                    "kind": "OBJECT",
                    "fields": [
                        {"name": "login"},
                        {"name": "deleteUser"},
                        {"name": "resetPassword"},
                        {"name": "updateProfile"},
                    ],
                },
                {"name": "User", "kind": "OBJECT", "fields": [{"name": "id"}, {"name": "email"}]},
                {"name": "String", "kind": "SCALAR", "fields": None},
            ],
        }
    }
}

_ROUTES = {
    "/graphql": json.dumps(_SCHEMA),
    "/nogql": json.dumps({"errors": [{"message": "introspection is not allowed"}]}),
    "/notjson": "this is not json at all",
}


def _serve():
    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            body = _ROUTES.get(self.path, "")
            self.send_response_only(200 if self.path in _ROUTES else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def _introspect(path: str):
    srv, port = _serve()
    try:
        return await graphql.graphql_introspect(f"http://127.0.0.1:{port}{path}")
    finally:
        srv.shutdown()


def _ids(out):
    return [f["id"] for f in out["findings"]]


async def test_introspection_enabled_maps_surface():
    out = await _introspect("/graphql")
    assert out["introspection_enabled"] is True and out["vulnerable"] is True
    assert set(out["queries"]) == {"me", "users", "product"}
    assert "deleteUser" in out["mutations"]
    assert "graphql-introspection-enabled" in _ids(out)


async def test_sensitive_mutations_flagged():
    out = await _introspect("/graphql")
    assert "graphql-sensitive-mutations" in _ids(out)
    assert {"deleteUser", "resetPassword", "updateProfile"} <= set(out["sensitive_mutations"])
    assert "login" not in out["sensitive_mutations"]  # benign mutation not flagged


async def test_introspection_disabled():
    out = await _introspect("/nogql")
    assert out["introspection_enabled"] is False and out["vulnerable"] is False
    assert out["findings"] == []


async def test_non_json_response_is_graceful():
    out = await _introspect("/notjson")
    assert out["introspection_enabled"] is False  # no schema → not enabled, no crash


async def test_unreachable_is_graceful():
    out = await graphql.graphql_introspect("http://127.0.0.1:1/graphql")
    assert "error" in out


async def test_no_url():
    assert "error" in await graphql.graphql_introspect("")


def test_graphql_introspect_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("graphql_introspect").auto_confirm is True  # one read-only POST → auto
