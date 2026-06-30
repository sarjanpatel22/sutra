"""Tests for the SSRF skill (friday/skills/ssrf.py) — hermetic server emulating a fetcher.

The server emulates an app that fetches whatever URL is in a parameter and reflects it: at the AWS
metadata IP it returns the metadata index; at file:///etc/passwd it returns passwd.
This exercises real payload delivery + signature detection, no network/cloud.
"""

from __future__ import annotations

import http.server
import threading
from urllib.parse import parse_qs, unquote, urlparse

from friday.skills import ssrf

_AWS_META = "ami-id\nhostname\niam/\ninstance-id\nlocal-ipv4\npublic-keys/\n"
_PASSWD = "root:x:0:0:root:/root:/bin/bash\n"


def _serve(vuln_param: str = "url"):
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            v = unquote(q.get(vuln_param, [""])[0]) if vuln_param else ""
            if "169.254.169.254" in v and "meta-data" in v:
                body = _AWS_META  # server fetched the metadata endpoint
            elif v.startswith("file:///etc/passwd"):
                body = _PASSWD  # server fetched a local file
            else:
                body = "<html>fetched nothing interesting</html>"
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


async def _check(path: str, vuln_param: str = "url", **kw):
    srv, port = _serve(vuln_param)
    try:
        return await ssrf.check_ssrf(f"http://127.0.0.1:{port}{path}", **kw)
    finally:
        srv.shutdown()


async def test_aws_metadata_ssrf_is_critical():
    out = await _check("/fetch?url=http://example.com")
    assert out["vulnerable"] is True
    f = out["findings"][0]
    assert f["id"] == "ssrf" and f["severity"] == "critical" and "AWS" in f["name"]


async def test_file_scheme_ssrf_detected():
    # only file:// reflects here → 'high' file-read SSRF
    out = await _check("/fetch?url=x", vuln_param="url", param="url")
    assert out["vulnerable"] is True
    assert any("file" in f["name"].lower() or "AWS" in f["name"] for f in out["findings"])


async def test_safe_endpoint_not_vulnerable():
    out = await _check("/fetch?url=x", vuln_param="")  # nothing is fetched
    assert out["vulnerable"] is False and out["findings"] == []


async def test_explicit_param():
    out = await _check("/x?target=1", vuln_param="target", param="target")
    assert out["vulnerable"] is True and out["hits"][0]["param"] == "target"


async def test_appended_param_when_none():
    out = await _check("/fetch", vuln_param="url")  # 'url' is in the default append set
    assert out["vulnerable"] is True and "url" in out["tested_params"]


async def test_unreachable_is_graceful():
    out = await ssrf.check_ssrf("http://127.0.0.1:1/?url=x")
    assert "error" in out


async def test_no_url():
    assert "error" in await ssrf.check_ssrf("")


def test_check_ssrf_is_gated():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("check_ssrf").auto_confirm is False  # active → gated
