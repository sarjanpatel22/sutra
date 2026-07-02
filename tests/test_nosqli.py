"""Tests for the NoSQLi skill (friday/skills/nosqli.py) — hermetic local servers.

Emulated apps: an error-based one that leaks a Mongo error when a payload breaks the query; an
operator one that actually interprets `param[$ne]` / `param[$eq]` (vulnerable); and a static one
that treats bracket keys as literal strings (NOT vulnerable → must not false-positive). Real payload
delivery over the loopback, no external network.
"""

from __future__ import annotations

import http.server
import threading
from urllib.parse import parse_qs, urlparse

from friday.skills import nosqli

_MONGO_ERR = "MongoServerError: unknown operator: $where — BadValue in query"
_FULL = "<html>" + "user-row " * 60 + "</html>"
_SHORT = "<html>no results</html>"


def _serve(mode: str, vuln_param: str = "user"):
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            body, code = _FULL, 200
            if mode == "error":
                raw = urlparse(self.path).query
                if "%27" in raw or "%22" in raw or "'" in raw or '"' in raw:  # a quote broke it
                    body, code = _MONGO_ERR, 500
            elif mode == "operator":  # actually interprets the injected operators
                if f"{vuln_param}[$eq]" in q:
                    body = _SHORT           # {$eq: nonexistent} → nothing
                elif f"{vuln_param}[$ne]" in q:
                    body = _FULL            # {$ne: nonexistent} → everything
                elif q.get(vuln_param, [""])[0] not in ("1", ""):
                    body = _SHORT           # a literal non-existent value → nothing
            elif mode == "static":  # ignores bracket keys entirely → not injectable
                body = _FULL
            data = body.encode()
            self.send_response_only(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def _check(mode: str, path: str, vuln_param: str = "user", **kw):
    srv, port = _serve(mode, vuln_param)
    try:
        return await nosqli.check_nosqli(f"http://127.0.0.1:{port}{path}", **kw)
    finally:
        srv.shutdown()


async def test_error_based_detected():
    out = await _check("error", "/item?user=1")
    assert out["vulnerable"] is True
    f = next(f for f in out["findings"] if f["id"] == "nosqli-error")
    assert f["severity"] == "high" and "MongoDB" in f["name"] and "confirmed" in f["name"]


async def test_operator_differential_detected():
    out = await _check("operator", "/item?user=1")
    assert out["vulnerable"] is True
    f = next(f for f in out["findings"] if f["id"] == "nosqli-operator")
    assert f["severity"] == "high" and "candidate" in f["name"]
    assert "[$ne]" in f["description"] or "$ne" in f["description"]


async def test_static_app_not_flagged():
    # bracket keys treated as literals → $ne and $eq responses identical → no false positive
    out = await _check("static", "/item?user=1")
    assert out["vulnerable"] is False
    assert out["findings"] == []


async def test_param_targeting():
    out = await _check("error", "/login?username=bob", vuln_param="username", param="username")
    assert out["tested_params"] == ["username"]
    assert out["vulnerable"] is True


async def test_unreachable_errors():
    # nothing listening on this port → clean error, no crash
    out = await nosqli.check_nosqli("http://127.0.0.1:1/item?user=1")
    assert "error" in out


async def test_empty_url_errors():
    assert "error" in await nosqli.check_nosqli("")


def test_skill_is_gated():
    from friday.skills import registry
    registry.autodiscover()
    assert registry.get("check_nosqli").auto_confirm is False
