"""Tests for the CRLF-injection skill (friday/skills/crlf.py) — hermetic vulnerable server.

The test server emulates a naive app that copies a parameter into response headers without stripping
CR-LF: it unquotes the value, splits on CR-LF, and emits each `k: v` line as a real header — exactly
the bug under test. So this exercises real payload delivery + header detection, no network.
"""

from __future__ import annotations

import http.server
import threading
from urllib.parse import parse_qs, unquote, urlparse

from friday.skills import crlf


def _serve(vuln_param: str = "q"):
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            raw = q.get(vuln_param, [""])[0] if vuln_param else ""
            val = unquote(raw)
            self.send_response_only(200)
            if vuln_param and ("\r\n" in val):  # vulnerable: input flows into headers unsanitised
                for line in val.split("\r\n"):
                    if ":" in line:
                        k, v = line.split(":", 1)
                        if k.strip():
                            self.send_header(k.strip(), v.strip())
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def _check(path: str, vuln_param: str = "q", **kw):
    srv, port = _serve(vuln_param)
    try:
        return await crlf.check_crlf(f"http://127.0.0.1:{port}{path}", **kw)
    finally:
        srv.shutdown()


async def test_crlf_marker_header_detected():
    out = await _check("/?q=home")  # 'q' is header-looking → tested
    assert out["vulnerable"] is True
    f = out["findings"][0]
    assert f["id"] == "crlf-injection" and f["severity"] == "high"
    assert crlf._MARKER in out["hits"][0]["evidence"].lower()


async def test_set_cookie_variant_detected():
    # force only the param so every payload is tried, including the Set-Cookie one
    out = await _check("/?q=x", vuln_param="q", param="q")
    assert out["vulnerable"] is True
    assert any("set-cookie" in h["evidence"].lower() or crlf._MARKER in h["evidence"].lower()
               for h in out["hits"])


async def test_explicit_param_targets_right_one():
    out = await _check("/?p=1", vuln_param="p", param="p")
    assert out["vulnerable"] is True and out["hits"][0]["param"] == "p"


async def test_appended_param_when_none_present():
    out = await _check("/page", vuln_param="q")  # no query; 'q' is in the default append set
    assert out["vulnerable"] is True and "q" in out["tested_params"]


async def test_safe_endpoint_not_vulnerable():
    out = await _check("/?q=home", vuln_param="")  # nothing flows into headers
    assert out["vulnerable"] is False and out["findings"] == []


async def test_unreachable_is_graceful():
    out = await crlf.check_crlf("http://127.0.0.1:1/?q=x")
    assert "error" in out


async def test_no_url():
    assert "error" in await crlf.check_crlf("")


def test_check_crlf_is_gated():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("check_crlf").auto_confirm is False  # active → gated
