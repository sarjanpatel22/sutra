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


# --- nmap_scan (mocked subprocess — no real nmap or network) ------------------------

async def test_nmap_scan_parses_services_and_surfaces_nse_vulns(monkeypatch):
    xml = (
        '<?xml version="1.0"?><nmaprun><host><status state="up"/>'
        '<address addr="10.0.0.5" addrtype="ipv4"/>'
        '<hostnames><hostname name="tgt.local"/></hostnames><ports>'
        '<port protocol="tcp" portid="22"><state state="open"/>'
        '<service name="ssh" product="OpenSSH" version="8.9"/></port>'
        '<port protocol="tcp" portid="80"><state state="open"/>'
        '<service name="http" product="nginx" version="1.18"/>'
        '<script id="http-vuln-cve2021-1234" output="VULNERABLE: path traversal"/></port>'
        '<port protocol="tcp" portid="443"><state state="closed"/></port>'
        '</ports></host></nmaprun>'
    )

    async def fake_run(cmd, timeout=20):
        assert cmd[0].endswith("nmap") and "-sV" in cmd and "-oX" in cmd and "-sC" in cmd
        assert cmd[-1] == "tgt.local"
        return (0, xml, "")

    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/bin/nmap")
    monkeypatch.setattr(recon, "_run", fake_run)
    out = await recon.nmap_scan("tgt.local", scripts="true")
    assert out["open_ports"] == 2                              # 22 + 80 open; 443 closed excluded
    host = out["hosts"][0]
    assert host["ip"] == "10.0.0.5" and host["hostname"] == "tgt.local"
    by_port = {p["port"]: p for p in host["open_ports"]}
    assert by_port[22]["product"] == "OpenSSH" and by_port[22]["version"] == "8.9"
    assert by_port[80]["service"] == "http"
    assert out["findings"][0]["matched_at"] == "10.0.0.5:80"   # NSE VULNERABLE → finding


async def test_nmap_scan_guards_and_missing_binary(monkeypatch):
    monkeypatch.setattr(recon.shutil, "which", lambda b: None)
    assert "not installed" in (await recon.nmap_scan("host"))["error"]
    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/bin/nmap")
    assert "invalid target" in (await recon.nmap_scan("-oN"))["error"]        # flag-injection guard
    assert "invalid port" in (await recon.nmap_scan("h", ports="80; rm"))["error"]  # bad port spec
    assert "no target" in (await recon.nmap_scan(""))["error"]
    assert recon._nmap_ports_ok("top") and recon._nmap_ports_ok("22,80,443")
    assert recon._nmap_ports_ok("1-1000") and not recon._nmap_ports_ok("80 rm")


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


# --- find_subdomains (passive OSINT — mocked subprocess) ----------------------------

async def test_find_subdomains_parses_and_cleans(monkeypatch):
    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/bin/subfinder")

    async def fake_run(cmd, timeout=20):
        assert "-d" in cmd and "example.com" in cmd     # URL was reduced to the bare domain
        return (0, "b.example.com\na.example.com\nA.EXAMPLE.COM\nnot a domain\n\n", "")

    monkeypatch.setattr(recon, "_run", fake_run)
    r = await recon.find_subdomains("https://example.com/path?q=1")
    assert r["domain"] == "example.com"
    assert r["subdomains"] == ["a.example.com", "b.example.com"]  # deduped/sorted, junk dropped
    assert r["count"] == 2


async def test_find_subdomains_missing_binary(monkeypatch):
    monkeypatch.setattr(recon.shutil, "which", lambda b: None)
    r = await recon.find_subdomains("example.com")
    assert "error" in r and "subfinder" in r["error"]


# --- http_probe (mocked httpx subprocess) -------------------------------------------

async def test_http_probe_parses_live_hosts(monkeypatch):
    j1 = json.dumps({"url": "http://a.com", "status_code": 200, "title": "A", "webserver": "nginx",
                     "tech": ["Nginx"], "content_length": 10, "host_ip": "1.1.1.1"})
    j2 = json.dumps({"url": "https://b.com", "status_code": 403, "title": "", "webserver": "Apache",
                     "tech": ["Apache HTTP Server"], "content_length": 5, "host_ip": "2.2.2.2",
                     "location": "https://b.com/login"})

    async def fake_run(cmd, timeout=20):
        assert "-json" in cmd and "-l" in cmd     # list-file + json mode
        return (0, j1 + "\n" + j2 + "\n", "")

    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/bin/httpx-toolkit")
    monkeypatch.setattr(recon, "_run", fake_run)
    out = await recon.http_probe("a.com, b.com, a.com")   # duplicate deduped by _http_targets
    assert out["probed"] == 2 and out["live"] == 2
    by_url = {x["url"]: x for x in out["results"]}
    a = by_url["http://a.com"]
    assert a["status"] == 200 and a["webserver"] == "nginx"
    assert by_url["https://b.com"]["location"] == "https://b.com/login"
    assert "Apache HTTP Server" in by_url["https://b.com"]["tech"]


async def test_http_probe_guards_and_missing_binary(monkeypatch):
    assert "error" in await recon.http_probe("")
    monkeypatch.setattr(recon.shutil, "which", lambda b: None)
    assert "not installed" in (await recon.http_probe("x.com"))["error"]
    assert recon._http_targets("a, b\nc  d") == ["a", "b", "c", "d"]
    assert len(recon._http_targets(",".join(str(i) for i in range(2000)))) == 1000  # capped


# --- sql_injection_test (mocked sqlmap subprocess) ----------------------------------

# A faithful slice of REAL sqlmap --batch output (captured live vs a local vulnerable endpoint).
_SQLMAP_VULN = """\
sqlmap identified the following injection point(s) with a total of 51 HTTP(s) requests:
---
Parameter: id (GET)
    Type: boolean-based blind
    Title: AND boolean-based blind - WHERE or HAVING clause
    Payload: id=1 AND 1234=1234
    Type: UNION query
    Title: Generic UNION query (NULL) - 2 columns
    Payload: id=1 UNION ALL SELECT NULL,NULL
---
back-end DBMS: SQLite
"""
_SQLMAP_SAFE = "all tested parameters do not appear to be injectable.\n"


async def test_sql_injection_test_confirms_and_builds_finding(monkeypatch):
    async def fake_run(cmd, timeout=20):
        # detection-only: never asks sqlmap to dump / enumerate / shell
        assert "--batch" in cmd and "--flush-session" in cmd
        assert not any(f in cmd for f in ("--dump", "--dbs", "--tables", "--os-shell"))
        return (0, _SQLMAP_VULN, "")

    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/bin/sqlmap")
    monkeypatch.setattr(recon, "_run", fake_run)
    out = await recon.sql_injection_test("http://site/item.php?id=1")
    assert out["injectable"] is True and out["dbms"] == "SQLite"
    p = out["parameters"][0]
    assert p["parameter"] == "id" and p["place"] == "GET"
    assert "boolean-based blind" in p["types"] and "UNION query" in p["types"]
    f = out["findings"][0]
    assert f["severity"] == "high" and "SQLite" in f["description"]
    assert "no data was dumped" in out["note"]


async def test_sql_injection_test_reports_clean_target(monkeypatch):
    async def fake_run(cmd, timeout=20):
        return (0, _SQLMAP_SAFE, "")

    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/bin/sqlmap")
    monkeypatch.setattr(recon, "_run", fake_run)
    out = await recon.sql_injection_test("http://site/item.php?id=1")
    assert out["injectable"] is False and "findings" not in out


async def test_sql_injection_test_guards_and_missing_binary(monkeypatch):
    assert "error" in await recon.sql_injection_test("")            # empty
    assert "invalid target" in (await recon.sql_injection_test("-hh"))["error"]  # flag-injection
    assert "invalid target" in (await recon.sql_injection_test("site.com/x?id=1"))["error"]
    monkeypatch.setattr(recon.shutil, "which", lambda b: None)
    assert "not installed" in (await recon.sql_injection_test("http://x/y?id=1"))["error"]
    # level/risk are bounded so a caller can't drive an abusive scan
    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/bin/sqlmap")

    async def cap_run(cmd, timeout=20):
        assert "--level=5" in cmd and "--risk=3" in cmd  # clamped from the out-of-range input below
        return (0, _SQLMAP_SAFE, "")

    monkeypatch.setattr(recon, "_run", cap_run)
    await recon.sql_injection_test("http://x/y?id=1", level=99, risk=99)


# --- nikto_scan (mocked nikto stdout report) ----------------------------------------

# A faithful slice of REAL nikto stdout (captured live). Meta lines + finding lines mixed; the
# parser must keep only the '/url: msg' findings and skip 'Target IP:' etc.
_NIKTO_OUT = """\
+ Target IP:          127.0.0.1
+ Target Port:        8099
+ Server: TestVuln/1.0 Python/3.13.12
+ [013587] /: Suggested security header missing: content-security-policy. See: https://x
+ [007352] /admin/: This might be interesting: outdated admin panel found. See: https://y
+ /backup.zip: Potentially interesting backup file found.
+ 8188 requests: 0 errors and 3 items reported on the remote host
+ End Time:           2026-07-20 17:01:02 (GMT-4) (2 seconds)
"""


async def test_nikto_scan_parses_findings_and_flags_risk(monkeypatch):
    async def fake_run(cmd, timeout=20):
        assert "-ask" in cmd and "no" in cmd  # never hang on the CIRT.net submit prompt
        assert "-maxtime" in cmd
        return (0, _NIKTO_OUT, "")

    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/bin/nikto")
    monkeypatch.setattr(recon, "_run", fake_run)
    out = await recon.nikto_scan("127.0.0.1", port="8099")
    assert out["server"] == "TestVuln/1.0 Python/3.13.12"
    assert out["items"] == 3  # 3 findings, meta lines (Target IP / requests / End Time) skipped
    by_url = {f["matched_at"]: f for f in out["findings"]}
    # 'outdated' + '/admin' + 'backup' hit the risk heuristic → medium; plain header note → low
    assert by_url["127.0.0.1/"]["severity"] == "low"
    assert by_url["127.0.0.1/admin/"]["severity"] == "medium"
    assert by_url["127.0.0.1/backup.zip"]["severity"] == "medium"


async def test_nikto_scan_guards_and_bounds(monkeypatch):
    assert "error" in await recon.nikto_scan("")               # empty
    assert "invalid target" in (await recon.nikto_scan("-h"))["error"]      # flag-injection
    assert "invalid port" in (await recon.nikto_scan("x", port="80a"))["error"]  # bad port
    monkeypatch.setattr(recon.shutil, "which", lambda b: None)
    assert "not installed" in (await recon.nikto_scan("x"))["error"]
    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/bin/nikto")

    async def cap_run(cmd, timeout=20):
        assert "900s" in cmd  # max_seconds clamped from the out-of-range value below
        return (0, "+ 0 items reported\n", "")

    monkeypatch.setattr(recon, "_run", cap_run)
    out = await recon.nikto_scan("x", max_seconds=99999)
    assert out["items"] == 0


def test_parse_nikto_skips_meta_lines():
    server, findings = recon._parse_nikto("+ Target IP: 1.2.3.4\n+ Start Time: now\n+ /x: hit\n")
    assert server == "" and len(findings) == 1 and findings[0]["url"] == "/x"


# --- credential_bruteforce (mocked hydra) -------------------------------------------

# A real hydra success line (captured live vs a local basic-auth endpoint).
_HYDRA_OUT = "[8098][http-get] host: 127.0.0.1   misc: /   login: admin   password: secret123\n"


async def test_credential_bruteforce_recovers_creds(monkeypatch):
    async def fake_run(cmd, timeout=20):
        assert "-f" in cmd and "-I" in cmd  # bounded: stop at first hit, ignore restore file
        assert cmd[-1] == "/"               # http-get path appended as the module arg
        assert "-l" in cmd and "admin" in cmd
        return (0, _HYDRA_OUT, "")

    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/bin/hydra")
    monkeypatch.setattr(recon, "_run", fake_run)
    out = await recon.credential_bruteforce(
        "127.0.0.1", "http-get", username="admin", wordlist="/tmp/pw.txt", port="8098")
    assert out["credentials_found"] == 1
    c = out["credentials"][0]
    assert c["username"] == "admin" and c["password"] == "secret123" and c["port"] == "8098"
    f = out["findings"][0]
    assert f["severity"] == "high" and "admin:secret123" in f["name"]
    assert "rotate" in out["note"]


async def test_credential_bruteforce_ssh_omits_http_arg(monkeypatch):
    async def fake_run(cmd, timeout=20):
        assert cmd[-1] == "ssh"  # non-http service → no trailing module arg
        return (0, "", "")  # no hit

    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/bin/hydra")
    monkeypatch.setattr(recon, "_run", fake_run)
    out = await recon.credential_bruteforce("h", "ssh", username="root", wordlist="/tmp/pw.txt")
    assert out["credentials_found"] == 0 and "No valid credentials" in out["note"]


async def test_credential_bruteforce_guards(monkeypatch):
    bad_t = await recon.credential_bruteforce("-x", "ssh", username="a")
    assert "invalid target" in bad_t["error"]
    bad = await recon.credential_bruteforce("h", "telnetX", username="a")
    assert "unsupported service" in bad["error"] and "ssh" in bad["supported"]
    assert "username or userlist" in (await recon.credential_bruteforce("h", "ssh"))["error"]
    flag = await recon.credential_bruteforce("h", "ssh", username="-oProxyCommand")
    assert "invalid username" in flag["error"]
    assert "invalid port" in (
        await recon.credential_bruteforce("h", "ssh", username="a", port="22x"))["error"]
    monkeypatch.setattr(recon.shutil, "which", lambda b: None)
    assert "not installed" in (
        await recon.credential_bruteforce("h", "ssh", username="a", wordlist="/w"))["error"]
    # no wordlist available anywhere → clear error, not a broken hydra call
    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/bin/hydra")
    monkeypatch.setattr(recon, "_default_crack_wordlist", lambda: None)
    assert "no wordlist" in (await recon.credential_bruteforce("h", "ssh", username="a"))["error"]


def test_parse_hydra_reads_hits():
    extra = "[22][ssh] host: 10.0.0.5   login: root   password: a b\n"
    creds = recon._parse_hydra(_HYDRA_OUT + extra)
    assert len(creds) == 2
    assert creds[1]["service"] == "ssh" and creds[1]["password"] == "a b"  # spaces preserved


# --- registration + gating ----------------------------------------------------------

def test_recon_skills_registered_with_correct_gating():
    from friday.skills import registry

    registry.autodiscover()
    for passive in ("dns_lookup", "whois", "searchsploit", "find_subdomains"):
        assert registry.get(passive).auto_confirm is True  # passive OSINT → auto
    assert registry.get("port_scan").auto_confirm is False  # active probe → gated
    assert registry.get("crack_hash").auto_confirm is False  # intensive crack → gated
    assert registry.get("discover_hosts").auto_confirm is False  # active sweep → gated
    assert registry.get("nmap_scan").auto_confirm is False  # active nmap scan → gated
    assert registry.get("http_probe").auto_confirm is False  # active http probe → gated
    assert registry.get("sql_injection_test").auto_confirm is False  # active SQLi exploit → gated
    assert registry.get("nikto_scan").auto_confirm is False  # active web-server scan → gated
    assert registry.get("credential_bruteforce").auto_confirm is False  # active brute → gated


# --- discover_hosts (hermetic — mocked socket layer) --------------------------------

def test_hosts_from_cidr_enumerates_and_caps():
    assert recon._hosts_from_cidr("10.0.0.0/30") == ["10.0.0.1", "10.0.0.2"]
    assert recon._hosts_from_cidr("10.0.0.0/16") == []   # too large → refused (max /24)
    assert recon._hosts_from_cidr("not-a-cidr") == []


async def test_discover_hosts_finds_only_live(monkeypatch):
    class _W:  # minimal writer
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def fake_open(host, port):
        if host == "10.0.0.5" and port == 80:
            return object(), _W()
        raise OSError("connection refused")

    monkeypatch.setattr(recon.asyncio, "open_connection", fake_open)
    out = await recon.discover_hosts(subnet="10.0.0.4/30", ports="22,80")
    assert out["subnet"] == "10.0.0.4/30" and out["scanned"] == 2
    assert out["live"] == [{"host": "10.0.0.5", "ports": [80]}]  # .6 answered nothing → omitted


async def test_discover_hosts_auto_detects_subnet(monkeypatch):
    monkeypatch.setattr(recon, "_local_subnet_24", lambda: "10.9.9.0/24")

    async def fake_open(host, port):
        raise OSError("refused")

    monkeypatch.setattr(recon.asyncio, "open_connection", fake_open)
    out = await recon.discover_hosts(ports="22")
    assert out["subnet"] == "10.9.9.0/24" and out["live"] == []


async def test_discover_hosts_rejects_bad_subnet(monkeypatch):
    monkeypatch.setattr(recon, "_local_subnet_24", lambda: None)
    assert "error" in await recon.discover_hosts(subnet="10.0.0.0/8")   # too large
    assert "error" in await recon.discover_hosts(subnet="")             # no subnet detectable


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


def test_default_wordlist_prefers_uncompressed(monkeypatch, tmp_path):
    real = tmp_path / "rockyou.txt"
    real.write_text("a\nb\n")
    monkeypatch.setattr(recon, "_CRACK_WORDLISTS", (str(real),))
    monkeypatch.setattr(recon, "_GZ_WORDLISTS", ("/nonexistent.gz",))
    assert recon._default_crack_wordlist() == str(real)  # no extraction when a real list exists


def test_default_wordlist_extracts_gzipped_rockyou(monkeypatch, tmp_path):
    # stock Kali: no uncompressed list, but rockyou.txt.gz is present → extract the full thing
    import gzip as _gz

    gzp = tmp_path / "rockyou.txt.gz"
    with _gz.open(gzp, "wt") as f:
        f.write("password\nletmein\nhunter2\n")
    cache = tmp_path / "cache" / "rockyou.txt"
    monkeypatch.setattr(recon, "_CRACK_WORDLISTS", ())
    monkeypatch.setattr(recon, "_FALLBACK_WORDLISTS", ())
    monkeypatch.setattr(recon, "_GZ_WORDLISTS", (str(gzp),))
    monkeypatch.setattr(recon, "_WORDLIST_CACHE", cache)

    assert recon._default_crack_wordlist() == str(cache)
    assert "hunter2" in cache.read_text()                       # full list extracted, not truncated
    assert recon._default_crack_wordlist() == str(cache)        # second call reuses the cache


def test_default_wordlist_none_when_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(recon, "_CRACK_WORDLISTS", ())
    monkeypatch.setattr(recon, "_GZ_WORDLISTS", ("/nonexistent.gz",))
    monkeypatch.setattr(recon, "_FALLBACK_WORDLISTS", ())
    monkeypatch.setattr(recon, "_WORDLIST_CACHE", tmp_path / "nope.txt")  # isolate from real cache
    assert recon._default_crack_wordlist() is None


async def test_crack_hash_passes_rules_for_tougher_passwords(monkeypatch, tmp_path):
    wl = tmp_path / "wl.txt"
    wl.write_text("raspberry\n")
    monkeypatch.setattr(recon.shutil, "which", lambda b: "/usr/sbin/john")
    seen = {}

    async def fake_run(cmd, timeout=20):
        if "--show" in cmd:
            return (0, "?:Raspberry1\n1 password hash cracked, 0 left\n", "")
        seen["crack"] = cmd  # the wordlist (crack) invocation
        return (0, "", "")

    monkeypatch.setattr(recon, "_run", fake_run)

    out = await recon.crack_hash("$6$x$y", wordlist=str(wl), rules="true")
    assert "--rules" in seen["crack"] and out["rules"] == "true" and out["password"] == "Raspberry1"

    await recon.crack_hash("$6$x$y", wordlist=str(wl), rules="Jumbo")  # a named ruleset
    assert "--rules=Jumbo" in seen["crack"]

    await recon.crack_hash("$6$x$y", wordlist=str(wl))  # default: no rules, unchanged behaviour
    assert not any(str(a).startswith("--rules") for a in seen["crack"])


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
