"""Tests for the path-traversal / LFI skill (friday/skills/path_traversal.py) — hermetic server.

The server only serves the /etc/passwd signature when the request actually carries a traversal
sequence in the targeted parameter, so this exercises real payload delivery + signature detection.
"""

from __future__ import annotations

import http.server
import threading
from urllib.parse import parse_qs, urlparse

from friday.skills import path_traversal

_PASSWD = "root:x:0:0:root:/root:/bin/bash\ndaemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"


def _serve(vuln_param: str = "file"):
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            val = (q.get(vuln_param, [""])[0]) if vuln_param else ""
            # naive: if the param value walks up and asks for passwd, "read" it (the bug under test)
            if "etc/passwd" in val.replace("%2e", ".").replace("%2f", "/"):
                body = _PASSWD
            else:
                body = "<html>welcome — nothing traversed</html>"
            data = body.encode()
            self.send_response_only(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def _check(path: str, vuln_param: str = "file", **kw):
    srv, port = _serve(vuln_param)
    try:
        return await path_traversal.check_path_traversal(f"http://127.0.0.1:{port}{path}", **kw)
    finally:
        srv.shutdown()


def _ids(out):
    return [f["id"] for f in out["findings"]]


async def test_traversal_detected_on_existing_param():
    out = await _check("/view?file=home")  # 'file' is file-looking → tested
    assert out["vulnerable"] is True
    f = next(f for f in out["findings"] if f["id"] == "path-traversal")
    assert f["severity"] == "high" and f["matched_at"]
    assert "root:x:0:0:" in out["hits"][0]["evidence"]
    assert out["hits"][0]["os"] == "linux"


async def test_explicit_param_targets_the_right_one():
    out = await _check("/p?q=1", vuln_param="q", param="q")
    assert out["vulnerable"] is True
    assert out["hits"][0]["param"] == "q"


async def test_appended_param_when_none_present():
    # no query at all; the skill appends its default set, which includes 'file'
    out = await _check("/get", vuln_param="file")
    assert out["vulnerable"] is True
    assert "file" in out["tested_params"]


async def test_clean_endpoint_no_findings():
    out = await _check("/view?file=home", vuln_param="")  # nothing ever reflects passwd
    assert out["vulnerable"] is False and out["findings"] == []


async def test_unreachable_is_graceful():
    out = await path_traversal.check_path_traversal("http://127.0.0.1:1/?file=x")
    assert "error" in out


async def test_no_url():
    assert "error" in await path_traversal.check_path_traversal("")


def test_payload_set_covers_encodings_and_targets():
    payloads = [p for p, _, _ in path_traversal._PAYLOADS]
    assert any("../" * 5 in p for p in payloads)  # plain traversal
    assert any("%252e" in p for p in payloads)  # double-encoded bypass
    assert any("....//" in p for p in payloads)  # dot-slash-stripping bypass
    assert any("win.ini" in p for p in payloads)  # windows target too


def test_check_path_traversal_is_gated():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("check_path_traversal").auto_confirm is False  # active → gated
