"""Tests for the SQLi skill (friday/skills/sqli.py) — hermetic servers.

Two emulated vulnerable apps: an error-based one that leaks a MySQL error when the value contains a
quote, and a boolean-based one whose page content depends on the injected condition. This exercises
real payload delivery + both detection paths, no network.
"""

from __future__ import annotations

import http.server
import threading
from urllib.parse import parse_qs, unquote, urlparse

from friday.skills import sqli

_MYSQL_ERR = (
    "You have an error in your SQL syntax; check the manual that corresponds to your MySQL "
    "server version for the right syntax near \"'\" at line 1"
)
_FULL = "<html>" + "item-row " * 60 + "</html>"
_SHORT = "<html>no results</html>"


def _serve(mode: str, vuln_param: str = "id"):
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            v = unquote(parse_qs(urlparse(self.path).query).get(vuln_param, [""])[0])
            body, code = "<html>product page</html>", 200
            if mode == "error" and ("'" in v or '"' in v):  # quote breaks the query
                body, code = _MYSQL_ERR, 500
            elif mode == "boolean":
                body = _SHORT if ("1=2" in v or "'1'='2" in v) else _FULL
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


async def _check(mode: str, path: str, vuln_param: str = "id", **kw):
    srv, port = _serve(mode, vuln_param)
    try:
        return await sqli.check_sqli(f"http://127.0.0.1:{port}{path}", **kw)
    finally:
        srv.shutdown()


async def test_error_based_detected():
    out = await _check("error", "/item?id=1")
    assert out["vulnerable"] is True
    f = next(f for f in out["findings"] if f["id"] == "sqli-error")
    assert f["severity"] == "high" and "MySQL" in f["name"] and "confirmed" in f["name"]


async def test_boolean_based_detected():
    out = await _check("boolean", "/item?id=1")
    assert out["vulnerable"] is True
    f = next(f for f in out["findings"] if f["id"] == "sqli-boolean")
    assert "candidate" in f["name"] and "boolean" in f["name"]


async def test_dynamic_page_not_false_flagged():
    # a page whose length is random each load must NOT be flagged as boolean SQLi
    import random

    class _Dyn(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = "<html>" + "x" * random.randint(50, 400) + "</html>"
            data = body.encode()
            self.send_response_only(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Dyn)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # several runs — the stability gate must keep all of them clean
        for _ in range(6):
            out = await sqli.check_sqli(f"http://127.0.0.1:{srv.server_address[1]}/item?id=1")
            assert out["vulnerable"] is False, "dynamic page false-flagged as SQLi"
    finally:
        srv.shutdown()


async def test_safe_endpoint_not_vulnerable():
    out = await _check("safe", "/item?id=1")
    assert out["vulnerable"] is False and out["findings"] == []


async def test_explicit_param():
    out = await _check("error", "/x?foo=1", vuln_param="foo", param="foo")
    assert out["vulnerable"] is True and out["findings"][0]["matched_at"]


async def test_appended_param_when_none():
    out = await _check("error", "/item", vuln_param="id")  # 'id' is in the default append set
    assert out["vulnerable"] is True and "id" in out["tested_params"]


async def test_db_error_ignored_if_in_baseline():
    # if the error string is always present, it must NOT be flagged (no differential)
    assert sqli._db_error(_MYSQL_ERR, _MYSQL_ERR) is None
    assert sqli._db_error(_MYSQL_ERR, "<html>clean</html>")[0] == "MySQL"


async def test_unreachable_is_graceful():
    out = await sqli.check_sqli("http://127.0.0.1:1/?id=1")
    assert "error" in out


async def test_no_url():
    assert "error" in await sqli.check_sqli("")


def test_check_sqli_is_gated():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("check_sqli").auto_confirm is False  # active → gated
