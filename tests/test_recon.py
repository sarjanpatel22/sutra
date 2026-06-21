"""Tests for the recon skills (friday/skills/recon.py).

No test needs a real binary or external network: port_scan runs against a local listening socket;
dns_lookup / whois / searchsploit go through a mocked subprocess and the missing-binary path.
"""

from __future__ import annotations

import asyncio
import json
import socket

from friday.skills import recon

# --- port_scan (hermetic — real local socket, no nmap) ------------------------------

async def test_port_scan_detects_open_and_skips_closed():
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    open_port = server.sockets[0].getsockname()[1]
    # a definitely-closed port: bind to grab a free number, release it immediately
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed_port = s.getsockname()[1]
    s.close()
    try:
        out = await recon.port_scan("127.0.0.1", f"{open_port},{closed_port}")
    finally:
        server.close()
        await server.wait_closed()
    open_found = {o["port"] for o in out["open"]}
    assert open_port in open_found
    assert closed_port not in open_found
    assert out["scanned"] == 2


async def test_port_scan_no_host_errors():
    assert "error" in await recon.port_scan("")


def test_parse_ports_forms():
    assert recon._parse_ports("top") == list(recon._TOP_PORTS)
    assert recon._parse_ports("22,80,443") == [22, 80, 443]
    assert recon._parse_ports("20-22") == [20, 21, 22]
    assert recon._parse_ports("nonsense") == []
    assert len(recon._parse_ports("1-5000")) <= 1024  # bounded


# --- dns_lookup ---------------------------------------------------------------------

async def test_dns_lookup_parses_dig_output(monkeypatch):
    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/bin/dig")

    async def fake_run(cmd, timeout=20):
        return (0, "93.184.216.34\n", "")

    monkeypatch.setattr(recon, "_run", fake_run)
    out = await recon.dns_lookup("example.com", "A")
    assert out["records"] == ["93.184.216.34"] and out["type"] == "A"


async def test_dns_lookup_bad_record_type():
    assert "error" in await recon.dns_lookup("example.com", "ZZZ")


async def test_dns_lookup_callable_via_registry_despite_name_param(monkeypatch):
    # The agent invokes every tool as registry.call(fn_name, **model_args). A skill with a
    # param literally named `name` (dns_lookup) must not collide with call()'s skill-name arg.
    from friday.skills import registry

    registry.autodiscover()
    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/bin/dig")

    async def fake_run(cmd, timeout=20):
        return (0, "1.2.3.4\n", "")

    monkeypatch.setattr(recon, "_run", fake_run)
    out = await registry.call("dns_lookup", name="example.com", record_type="A")
    assert out["records"] == ["1.2.3.4"]


async def test_dns_lookup_without_dig_cannot_do_mx(monkeypatch):
    monkeypatch.setattr(recon.shutil, "which", lambda b: None)
    out = await recon.dns_lookup("example.com", "MX")
    assert "error" in out and "dig" in out["error"]


# --- whois --------------------------------------------------------------------------

async def test_whois_parses_fields(monkeypatch):
    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/bin/whois")
    sample = "Registrar: Example Registrar, Inc.\nCreation Date: 1995-08-14T04:00:00Z\n"

    async def fake_run(cmd, timeout=20):
        return (0, sample, "")

    monkeypatch.setattr(recon, "_run", fake_run)
    out = await recon.whois("example.com")
    assert out["fields"]["Registrar"] == "Example Registrar, Inc."
    assert "Creation Date" in out["fields"]


async def test_whois_missing_binary(monkeypatch):
    monkeypatch.setattr(recon.shutil, "which", lambda b: None)
    out = await recon.whois("example.com")
    assert "error" in out and "whois" in out["error"]


# --- searchsploit -------------------------------------------------------------------

async def test_searchsploit_parses_json(monkeypatch):
    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/bin/searchsploit")
    payload = json.dumps({"RESULTS_EXPLOIT": [
        {"Title": "vsftpd 2.3.4 - Backdoor Command Execution", "Path": "/x.rb", "EDB-ID": "17491"},
    ]})

    async def fake_run(cmd, timeout=20):
        return (0, payload, "")

    monkeypatch.setattr(recon, "_run", fake_run)
    out = await recon.searchsploit("vsftpd 2.3.4")
    assert out["count"] == 1
    assert out["results"][0]["edb_id"] == "17491"


async def test_searchsploit_missing_binary(monkeypatch):
    monkeypatch.setattr(recon.shutil, "which", lambda b: None)
    assert "error" in await recon.searchsploit("anything")


# --- registration + gating ----------------------------------------------------------

def test_recon_skills_registered_with_correct_gating():
    from friday.skills import registry

    registry.autodiscover()
    for passive in ("dns_lookup", "whois", "searchsploit"):
        assert registry.get(passive).auto_confirm is True
    assert registry.get("port_scan").auto_confirm is False  # active probe → gated
    assert registry.get("crack_hash").auto_confirm is False  # intensive crack → gated


# --- crack_hash ---------------------------------------------------------------------

def test_parse_john_show():
    assert recon._parse_john_show("?:secret\n1 password hash cracked, 0 left") == "secret"
    assert recon._parse_john_show("user:hunter2\n") == "hunter2"
    assert recon._parse_john_show("0 password hashes cracked, 1 left") is None
    assert recon._parse_john_show("") is None


async def test_crack_hash_returns_password(monkeypatch, tmp_path):
    wl = tmp_path / "wl.txt"
    wl.write_text("password\n")
    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/sbin/john")

    async def fake_run(cmd, timeout=20):
        if "--show" in cmd:
            return (0, "?:password\n\n1 password hash cracked, 0 left\n", "")
        return (0, "password   (?)\n", "")

    monkeypatch.setattr(recon, "_run", fake_run)
    out = await recon.crack_hash("5f4dcc3b5aa765d61d8327deb882cf99", wordlist=str(wl))
    assert out["cracked"] is True and out["password"] == "password"


async def test_crack_hash_reports_uncracked(monkeypatch, tmp_path):
    wl = tmp_path / "wl.txt"
    wl.write_text("nope\n")
    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/sbin/john")

    async def fake_run(cmd, timeout=20):
        return (0, "0 password hashes cracked, 1 left\n", "")

    monkeypatch.setattr(recon, "_run", fake_run)
    out = await recon.crack_hash("deadbeefdeadbeef", wordlist=str(wl))
    assert out["cracked"] is False and "password" not in out


async def test_crack_hash_missing_john(monkeypatch, tmp_path):
    wl = tmp_path / "wl.txt"
    wl.write_text("a\n")
    monkeypatch.setattr(recon.shutil, "which", lambda b: None)
    out = await recon.crack_hash("x", wordlist=str(wl))
    assert "error" in out and "john" in out["error"]


async def test_crack_hash_no_wordlist(monkeypatch):
    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/sbin/john")
    monkeypatch.setattr(recon, "_default_crack_wordlist", lambda: None)
    out = await recon.crack_hash("x")
    assert "error" in out and "wordlist" in out["error"]
