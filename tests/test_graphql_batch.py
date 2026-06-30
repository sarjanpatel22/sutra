"""Tests for the GraphQL batching skill (friday/skills/graphql_batch.py) — hermetic server.

The server emulates a GraphQL endpoint: it resolves aliased __typename fields and array-batched
queries, so both batching-detection paths are exercised. A 'safe' mode rejects batching.
"""

from __future__ import annotations

import http.server
import json
import threading

from friday.skills import graphql_batch


def _serve(mode: str = "vuln"):
    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n).decode()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, list):  # array batch
                if mode == "vuln":
                    out = [{"data": {"__typename": "Query"}} for _ in payload]
                else:
                    out = {"errors": [{"message": "batching disabled"}]}
            else:  # single query, possibly with aliases
                q = payload.get("query", "")
                data = {}
                if mode == "vuln":
                    for tok in q.replace("{", " ").replace("}", " ").split():
                        if tok.endswith(":__typename"):
                            data[tok.split(":")[0]] = "Query"
                    if "__typename" in q and not data:
                        data["__typename"] = "Query"
                else:
                    data = {"__typename": "Query"}  # resolves single, no aliases honored
                out = {"data": data}
            body = json.dumps(out).encode()
            self.send_response_only(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def _check(mode: str):
    srv = _serve(mode)
    try:
        return await graphql_batch.graphql_batch(f"http://127.0.0.1:{srv.server_address[1]}/graphql")
    finally:
        srv.shutdown()


async def test_detects_alias_and_array_batching():
    out = await _check("vuln")
    assert out["vulnerable"] is True
    assert out["alias_batching"] is True and out["array_batching"] is True
    ids = {f["id"] for f in out["findings"]}
    assert "graphql-alias-batching" in ids and "graphql-array-batching" in ids


async def test_safe_endpoint_not_flagged():
    out = await _check("safe")
    assert out["vulnerable"] is False and out["findings"] == []


async def test_unreachable_is_graceful():
    out = await graphql_batch.graphql_batch("http://127.0.0.1:1/graphql")
    assert "error" in out


async def test_no_url():
    assert "error" in await graphql_batch.graphql_batch("")


def test_graphql_batch_is_gated():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("graphql_batch").auto_confirm is False  # active POSTs → gated
