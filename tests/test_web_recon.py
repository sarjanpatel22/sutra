"""Tests for the web-recon skills (friday/skills/web_recon.py).

http_headers runs against a real LOCAL server (hermetic, no network); web_enum is driven through a
mocked subprocess + the graceful no-tool / no-wordlist paths (no gobuster/ffuf needed in CI).
"""

from __future__ import annotations

import http.server
import threading

from friday.skills import web_recon

# --- http_headers (hermetic local server) -------------------------------------------

def _serve(headers: dict[str, str]):
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response_only(200)  # no auto Server/Date — so our headers stand
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def test_http_headers_reports_server_and_missing_security():
    srv, port = _serve({"Server": "TestServer/1.0", "X-Frame-Options": "DENY"})
    try:
        out = await web_recon.http_headers(f"http://127.0.0.1:{port}")
    finally:
        srv.shutdown()
    assert out["status"] == 200
    assert out["server"] == "TestServer/1.0"
    assert "X-Frame-Options" not in out["missing_security_headers"]      # present → not flagged
    assert "Content-Security-Policy" in out["missing_security_headers"]  # absent → flagged


async def test_http_headers_bad_host_returns_error():
    # 127.0.0.1:1 — nothing listens → graceful error, not a raise
    out = await web_recon.http_headers("http://127.0.0.1:1")
    assert "error" in out


async def test_http_headers_no_url():
    assert "error" in await web_recon.http_headers("")


# --- web_enum -----------------------------------------------------------------------

async def test_web_enum_parses_gobuster_output(monkeypatch, tmp_path):
    wl = tmp_path / "wl.txt"
    wl.write_text("admin\nlogin\n")
    monkeypatch.setattr(web_recon.shutil, "which",
                        lambda b: "/usr/bin/gobuster" if b == "gobuster" else None)

    async def fake_run(cmd, timeout=20):
        return (0, "/admin (Status: 301) [Size: 312]\n/login (Status: 200) [Size: 1024]\n", "")

    monkeypatch.setattr(web_recon, "_run", fake_run)
    out = await web_recon.web_enum("http://10.10.1.1", "dirs", wordlist=str(wl))
    assert out["tool"] == "gobuster" and out["count"] == 2
    assert out["found"][0] == {"path": "/admin", "status": 301, "size": 312}


async def test_web_enum_defaults_bare_host_to_http(monkeypatch, tmp_path):
    wl = tmp_path / "wl.txt"
    wl.write_text("a\n")
    seen: dict = {}
    monkeypatch.setattr(web_recon.shutil, "which", lambda b: "/usr/bin/gobuster")

    async def fake_run(cmd, timeout=20):
        seen["cmd"] = cmd
        return (0, "", "")

    monkeypatch.setattr(web_recon, "_run", fake_run)
    await web_recon.web_enum("10.10.1.1", wordlist=str(wl))
    assert "http://10.10.1.1" in seen["cmd"]


async def test_web_enum_no_tool_installed(monkeypatch, tmp_path):
    wl = tmp_path / "wl.txt"
    wl.write_text("a\n")
    monkeypatch.setattr(web_recon.shutil, "which", lambda b: None)
    out = await web_recon.web_enum("http://x", wordlist=str(wl))
    assert "error" in out and "gobuster" in out["error"]


async def test_web_enum_no_wordlist(monkeypatch):
    monkeypatch.setattr(web_recon, "_default_wordlist", lambda: None)
    out = await web_recon.web_enum("http://x")
    assert "error" in out and "wordlist" in out["error"]


async def test_web_enum_bad_mode(tmp_path):
    wl = tmp_path / "wl.txt"
    wl.write_text("a\n")
    out = await web_recon.web_enum("http://x", "explode", wordlist=str(wl))
    assert "error" in out and "mode" in out["error"]


def test_web_recon_gating():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("web_enum").auto_confirm is False    # active brute → gated
    assert registry.get("http_headers").auto_confirm is True  # passive GET → auto
