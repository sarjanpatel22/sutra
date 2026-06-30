"""Tests for the XXE skill (friday/skills/xxe.py) — hermetic server emulating a vulnerable parser.

The server emulates an XML parser that resolves external entities: when the POST body declares a
SYSTEM entity for /etc/passwd and expands it, the server returns the file contents — exactly what a
vulnerable parser would do. This exercises real payload construction + signature detection, no I/O.
"""

from __future__ import annotations

import http.server
import threading

from friday.skills import xxe

_PASSWD = "root:x:0:0:root:/root:/bin/bash\ndaemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"


def _serve(vulnerable: bool = True):
    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode("utf-8", "replace")
            out = "<sutra></sutra>"
            if vulnerable and 'SYSTEM "file:///etc/passwd"' in body and "&xxe;" in body:
                out = f"<sutra>{_PASSWD}</sutra>"  # parser resolved the external entity
            data = out.encode()
            self.send_response_only(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def _check(vulnerable: bool = True):
    srv, port = _serve(vulnerable)
    try:
        return await xxe.check_xxe(f"http://127.0.0.1:{port}/api/xml")
    finally:
        srv.shutdown()


async def test_xxe_file_read_detected():
    out = await _check(vulnerable=True)
    assert out["vulnerable"] is True
    f = out["findings"][0]
    assert f["id"] == "xxe" and f["severity"] == "high"
    assert out["hits"][0]["os"] == "linux"
    assert "root:x:0:0:" in out["hits"][0]["evidence"]


async def test_non_vulnerable_endpoint():
    out = await _check(vulnerable=False)
    assert out["vulnerable"] is False and out["findings"] == []


def test_payload_is_wellformed_xxe():
    body = xxe._payload("file:///etc/passwd")
    assert "<!DOCTYPE" in body and "<!ENTITY xxe SYSTEM" in body and "&xxe;" in body


async def test_unreachable_is_graceful():
    out = await xxe.check_xxe("http://127.0.0.1:1/api")
    assert "error" in out


async def test_no_url():
    assert "error" in await xxe.check_xxe("")


def test_check_xxe_is_gated():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("check_xxe").auto_confirm is False  # active → gated
