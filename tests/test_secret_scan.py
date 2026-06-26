"""Tests for the exposed-secret scanner (friday/skills/secret_scan.py) — hermetic local server."""

from __future__ import annotations

import http.server
import threading

from friday.skills import secret_scan

_GKEY = "AIza" + "0" * 35  # google-api-key shape
_AWS = "AKIAIOSFODNN7EXAMPLE"  # aws-access-key-id shape
_STRIPE = "sk_live_" + "0123456789abcdefABCDEFxyz0"  # stripe-secret-key shape (>=24 after prefix)

_PAGE = (
    f'<html><head><script src="/app.js"></script></head><body>'
    f'<img src="https://maps.googleapis.com/maps/api/js?key={_GKEY}"></body></html>'
)
_JS = f'var a="{_AWS}";var s="{_STRIPE}";'
_DUP = f'<html><script src="/app.js"></script>secret={_AWS}</html>'  # same AWS key as the JS
_GENERIC = '<html><script>var c={password:"supersecretvalue123"}</script></html>'

_ROUTES = {
    "/": ("text/html", _PAGE),
    "/app.js": ("application/javascript", _JS),
    "/clean": ("text/html", "<html><body>nothing to see</body></html>"),
    "/dup": ("text/html", _DUP),
    "/generic": ("text/html", _GENERIC),
}


def _serve():
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            ct, body = _ROUTES.get(self.path, ("text/plain", ""))
            code = 200 if self.path in _ROUTES else 404
            self.send_response_only(code)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def _scan(path: str, **kw):
    srv, port = _serve()
    try:
        return await secret_scan.scan_secrets(f"http://127.0.0.1:{port}{path}", **kw)
    finally:
        srv.shutdown()


def _ids(out):
    return [f["id"] for f in out["findings"]]


async def test_finds_secrets_in_page_and_linked_js():
    out = await _scan("/")
    ids = _ids(out)
    assert "exposed-google-api-key" in ids  # inline in the page
    assert "exposed-aws-access-key-id" in ids  # pulled from the linked JS
    assert "exposed-stripe-secret-key" in ids  # critical, from the JS
    assert out["vulnerable"] is True
    aws = next(f for f in out["findings"] if f["id"] == "exposed-aws-access-key-id")
    assert any("app.js" in s for s in aws["sources"])  # source attributed to the JS file


async def test_stripe_key_is_critical_and_sorted_first():
    out = await _scan("/")
    assert out["findings"][0]["severity"] == "critical"  # worst-first ordering


async def test_clean_page_has_no_findings():
    out = await _scan("/clean")
    assert out["count"] == 0 and out["vulnerable"] is False


async def test_dedup_same_secret_lists_multiple_sources():
    out = await _scan("/dup")  # AWS key inline AND in the linked JS
    aws = [f for f in out["findings"] if f["id"] == "exposed-aws-access-key-id"]
    assert len(aws) == 1 and len(aws[0]["sources"]) == 2  # one finding, two sources


async def test_max_js_zero_scans_only_the_page():
    out = await _scan("/", max_js=0)
    ids = _ids(out)
    assert "exposed-google-api-key" in ids  # inline still found
    assert "exposed-aws-access-key-id" not in ids  # JS not fetched
    assert len(out["scanned"]) == 1


async def test_generic_assignment_detected():
    out = await _scan("/generic")
    assert "exposed-generic-secret" in _ids(out)


async def test_unreachable_is_graceful():
    out = await secret_scan.scan_secrets("http://127.0.0.1:1/")
    assert "error" in out


async def test_no_url():
    assert "error" in await secret_scan.scan_secrets("")


def test_scan_secrets_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("scan_secrets").auto_confirm is True  # passive GETs → auto
