"""Tests for the crt.sh subdomain skill (friday/skills/crtsh.py) — hermetic CT server.

A local server stands in for crt.sh (the skill's _CRTSH base is pointed at it), returning CT-style
JSON so name extraction, wildcard stripping, scope filtering, and dedupe are exercised — no network.
"""

from __future__ import annotations

import http.server
import json
import threading

from friday.skills import crtsh

_CT_JSON = [
    {"name_value": "www.x.com\n*.x.com", "common_name": "www.x.com"},
    {"name_value": "staging.x.com", "common_name": "staging.x.com"},
    {"name_value": "vpn.internal.x.com", "common_name": "vpn.internal.x.com"},
    {"name_value": "x.com", "common_name": "x.com"},
    {"name_value": "notours.evil.com", "common_name": "notours.evil.com"},  # out of scope → drop
    {"name_value": "admin@x.com", "common_name": ""},                        # email → drop
]


def _serve():
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            data = json.dumps(_CT_JSON).encode()
            self.send_response_only(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def _run(monkeypatch):
    srv = _serve()
    monkeypatch.setattr(crtsh, "_CRTSH", f"http://127.0.0.1:{srv.server_address[1]}/")
    try:
        return await crtsh.crtsh_subdomains("x.com")
    finally:
        srv.shutdown()


async def test_extracts_and_filters(monkeypatch):
    out = await _run(monkeypatch)
    subs = out["subdomains"]
    assert "www.x.com" in subs
    assert "staging.x.com" in subs
    assert "vpn.internal.x.com" in subs  # deep subdomain kept
    assert "x.com" in subs               # apex kept
    assert "notours.evil.com" not in subs  # out-of-scope dropped
    assert not any("@" in s for s in subs)  # emails dropped


async def test_wildcards_stripped_and_deduped(monkeypatch):
    out = await _run(monkeypatch)
    assert not any(s.startswith("*") for s in out["subdomains"])
    # www.x.com appears in two fields/rows but counted once
    assert out["subdomains"].count("www.x.com") == 1


async def test_bare_normalization():
    assert crtsh._bare("https://*.API.X.com/p") == "api.x.com"


async def test_unreachable_is_graceful(monkeypatch):
    monkeypatch.setattr(crtsh, "_CRTSH", "http://127.0.0.1:1/")
    out = await crtsh.crtsh_subdomains("x.com")
    assert "error" in out


async def test_no_domain():
    assert "error" in await crtsh.crtsh_subdomains("")


def test_crtsh_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("crtsh_subdomains").auto_confirm is True  # queries crt.sh, not the target
