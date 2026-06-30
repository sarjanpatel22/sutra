"""Tests for the crt.sh subdomain skill (friday/skills/crtsh.py) — hermetic CT servers.

Local servers stand in for crt.sh and certspotter (the skill's _CRTSH/_CERTSPOTTER bases are pointed
at them), so name extraction, wildcard stripping, scope filtering, dedupe, the certspotter fallback,
and retry/graceful-failure are all exercised — no network.
"""

from __future__ import annotations

import http.server
import json
import threading

from friday.skills import crtsh

_CRTSH_JSON = [
    {"name_value": "www.x.com\n*.x.com", "common_name": "www.x.com"},
    {"name_value": "staging.x.com", "common_name": "staging.x.com"},
    {"name_value": "x.com", "common_name": "x.com"},
    {"name_value": "notours.evil.com", "common_name": "notours.evil.com"},  # out of scope → drop
    {"name_value": "admin@x.com", "common_name": ""},                        # email → drop
]
_CERTSPOTTER_JSON = [
    {"dns_names": ["www.x.com", "vpn.x.com"]},   # vpn only here → proves union/fallback
    {"dns_names": ["*.api.x.com"]},
]


def _server(payload, status=200):
    body = json.dumps(payload).encode()

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response_only(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if status == 200:
                self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _point(monkeypatch, crt_srv, cs_srv):
    if crt_srv:
        monkeypatch.setattr(crtsh, "_CRTSH", f"http://127.0.0.1:{crt_srv.server_address[1]}/")
    if cs_srv:
        monkeypatch.setattr(crtsh, "_CERTSPOTTER", f"http://127.0.0.1:{cs_srv.server_address[1]}/")


async def test_merges_both_sources(monkeypatch):
    crt, cs = _server(_CRTSH_JSON), _server(_CERTSPOTTER_JSON)
    _point(monkeypatch, crt, cs)
    try:
        out = await crtsh.crtsh_subdomains("x.com")
    finally:
        crt.shutdown()
        cs.shutdown()
    subs = out["subdomains"]
    assert "www.x.com" in subs and "staging.x.com" in subs and "x.com" in subs  # crt.sh
    assert "vpn.x.com" in subs and "api.x.com" in subs                          # certspotter union
    assert "notours.evil.com" not in subs and not any("@" in s for s in subs)
    assert not any(s.startswith("*") for s in subs)                             # wildcards stripped
    assert set(out["sources"]) == {"crt.sh", "certspotter"}


async def test_certspotter_fallback_when_crtsh_down(monkeypatch):
    # crt.sh 502s; certspotter still answers → still get results (the net-flaky fix)
    crt_down, cs = _server({}, status=502), _server(_CERTSPOTTER_JSON)
    _point(monkeypatch, crt_down, cs)
    try:
        out = await crtsh.crtsh_subdomains("x.com")
    finally:
        crt_down.shutdown()
        cs.shutdown()
    assert "vpn.x.com" in out["subdomains"]            # fell back to certspotter
    assert out["sources"] == ["certspotter"]
    assert "crt.sh" in out["errors"]                   # the failure is surfaced, not silent


async def test_all_sources_down_is_error(monkeypatch):
    monkeypatch.setattr(crtsh, "_CRTSH", "http://127.0.0.1:1/")
    monkeypatch.setattr(crtsh, "_CERTSPOTTER", "http://127.0.0.1:1/")
    out = await crtsh.crtsh_subdomains("x.com")
    assert "error" in out and "all CT sources failed" in out["error"]


def test_parse_certspotter():
    names = crtsh._parse_certspotter(json.dumps(_CERTSPOTTER_JSON), "x.com")
    assert names == {"www.x.com", "vpn.x.com", "api.x.com"}


async def test_bare_normalization():
    assert crtsh._bare("https://*.API.X.com/p") == "api.x.com"


async def test_no_domain():
    assert "error" in await crtsh.crtsh_subdomains("")


def test_crtsh_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("crtsh_subdomains").auto_confirm is True
