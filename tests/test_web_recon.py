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

async def _no_wildcard(url):  # hermetic: skip the real catch-all HTTP probe in tests
    return None


async def test_web_enum_parses_gobuster_output(monkeypatch, tmp_path):
    wl = tmp_path / "wl.txt"
    wl.write_text("admin\nlogin\n")
    monkeypatch.setattr(web_recon.shutil, "which",
                        lambda b: "/usr/bin/gobuster" if b == "gobuster" else None)
    monkeypatch.setattr(web_recon, "_wildcard_len", _no_wildcard)

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
    monkeypatch.setattr(web_recon, "_wildcard_len", _no_wildcard)

    async def fake_run(cmd, timeout=20):
        seen["cmd"] = cmd
        return (0, "", "")

    monkeypatch.setattr(web_recon, "_run", fake_run)
    await web_recon.web_enum("10.10.1.1", wordlist=str(wl))
    assert "http://10.10.1.1" in seen["cmd"]


async def test_web_enum_excludes_spa_wildcard_length(monkeypatch, tmp_path):
    # the live-caught SPA case: every path 200s the app shell → exclude that length so real paths
    # (a /ftp dir of a different size) still surface instead of gobuster aborting
    wl = tmp_path / "wl.txt"
    wl.write_text("ftp\n")
    seen: dict = {}
    monkeypatch.setattr(web_recon.shutil, "which", lambda b: "/usr/bin/gobuster")

    async def spa(url):
        return 9903                                          # catch-all 200 body length

    monkeypatch.setattr(web_recon, "_wildcard_len", spa)

    async def fake_run(cmd, timeout=20):
        seen["cmd"] = cmd
        return (0, "/ftp (Status: 200) [Size: 512]\n", "")

    monkeypatch.setattr(web_recon, "_run", fake_run)
    out = await web_recon.web_enum("http://spa", wordlist=str(wl))
    assert "--exclude-length" in seen["cmd"] and "9903" in seen["cmd"]    # shell length filtered
    assert out["found"][0]["path"] == "/ftp"                             # the real path surfaces


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


# --- nuclei_scan (the bug-bounty find step — mocked subprocess) --------------------------

async def test_nuclei_scan_parses_and_sorts_findings(monkeypatch):
    import json

    monkeypatch.setattr(web_recon.shutil, "which", lambda b: "/usr/bin/nuclei")
    sample = "\n".join([
        json.dumps({"template-id": "tech-detect", "matched-at": "http://t",
                    "info": {"name": "Tech", "severity": "info", "tags": ["tech"]}}),
        json.dumps({"template-id": "prometheus-metrics", "matched-at": "http://t/metrics",
                    "info": {"name": "Exposed Metrics", "severity": "medium",
                             "tags": ["exposure"]}}),
        "[INF] a stray log line that is not json",
    ])

    async def fake_run(cmd, timeout=20):
        assert "-jsonl" in cmd and "http://t" in cmd
        return (0, sample, "")

    monkeypatch.setattr(web_recon, "_run", fake_run)
    r = await web_recon.nuclei_scan("t")                       # bare host → http://
    assert r["target"] == "http://t" and r["count"] == 2      # the log line is skipped
    assert r["findings"][0]["id"] == "prometheus-metrics"     # sorted worst-first (medium > info)
    assert r["findings"][0]["severity"] == "medium" and r["findings"][0]["tags"] == ["exposure"]


async def test_nuclei_scan_recovers_partial_findings_on_timeout(monkeypatch):
    # the killer gap: a scan that finds bugs then TIMES OUT must not lose them. nuclei writes to
    # the -o file as it goes; we read that file even when _run discards stdout on timeout.
    import json
    from pathlib import Path

    monkeypatch.setattr(web_recon.shutil, "which", lambda b: "/usr/bin/nuclei")
    finding = json.dumps({"template-id": "exposed-secret", "matched-at": "http://t/.env",
                          "info": {"name": "Exposed env", "severity": "high"}})

    async def fake_run(cmd, timeout=20):
        Path(cmd[cmd.index("-o") + 1]).write_text(finding + "\n")  # nuclei wrote it pre-kill
        return (-1, "", "timed out after 285s")                    # run TIMED OUT, no stdout

    monkeypatch.setattr(web_recon, "_run", fake_run)
    r = await web_recon.nuclei_scan("http://t")
    assert r["count"] == 1 and r["findings"][0]["id"] == "exposed-secret"  # recovered, not lost!
    assert r["incomplete"] is True and "TIMED OUT" in r["note"]            # but flagged as partial


async def test_nuclei_scan_active_runs_dast(monkeypatch):
    # active=True flips nuclei into DAST/fuzzing mode (SSRF/injection), and drops the passive
    # profile tags (they don't apply to the fuzzing template set)
    monkeypatch.setattr(web_recon.shutil, "which", lambda b: "/usr/bin/nuclei")
    seen: dict = {}

    async def fake_run(cmd, timeout=20):
        seen["cmd"] = cmd
        return (0, "", "")

    monkeypatch.setattr(web_recon, "_run", fake_run)
    await web_recon.nuclei_scan("http://t/page?id=1", tags="exposure", active=True)
    assert "-dast" in seen["cmd"]                          # active testing engine on
    assert "-tags" not in seen["cmd"]                      # passive profile tags dropped


async def test_nuclei_scan_missing_binary(monkeypatch):
    monkeypatch.setattr(web_recon.shutil, "which", lambda b: None)
    r = await web_recon.nuclei_scan("http://t")
    assert "error" in r and "nuclei" in r["error"]


async def test_nuclei_scan_no_findings_flags_templates(monkeypatch):
    monkeypatch.setattr(web_recon.shutil, "which", lambda b: "/usr/bin/nuclei")

    async def fake_run(cmd, timeout=20):
        return (0, "", "no templates found to execute")

    monkeypatch.setattr(web_recon, "_run", fake_run)
    r = await web_recon.nuclei_scan("http://t")
    assert r["count"] == 0 and "update-templates" in r["note"]


def test_web_recon_gating():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("web_enum").auto_confirm is False    # active brute → gated
    assert registry.get("http_headers").auto_confirm is True  # passive GET → auto
    assert registry.get("nuclei_scan").auto_confirm is False  # active vuln scan → gated
