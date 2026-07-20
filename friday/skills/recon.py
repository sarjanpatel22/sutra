"""Recon skills — DNS, WHOIS, port scan, exploit search: the passive-to-active recon primitives
for TryHackMe / authorized pentest.

dns_lookup / whois / searchsploit shell out to system tools (dig / whois / searchsploit) and
degrade gracefully to a clear "X not installed" error when the binary is absent — so a clean CI
install never breaks (these binaries live on the Kali box, not in CI). port_scan is a pure-stdlib
async TCP-connect scan: no nmap, no root, works everywhere — fast open/closed triage (use nmap via
run_command for version detection).

port_scan is the one ACTIVE probe here, so it is auto_confirm=False (gated like run_command); the
rest are passive public lookups (auto_confirm=True). Authorized targets only.
"""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Any

from friday.skills.registry import skill


async def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str, str]:
    """Run a command, returning (returncode, stdout, stderr). rc=-1 on timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as e:
        return -1, "", str(e)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"timed out after {timeout}s"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


_DNS_TYPES = ("A", "AAAA", "MX", "NS", "TXT", "CNAME", "SOA", "PTR")


@skill(
    name="dns_lookup",
    description=(
        "Look up DNS records for a domain/host — A, AAAA, MX, NS, TXT, CNAME, SOA, PTR. For recon "
        "(find a target's IPs, mail/name servers, SPF/TXT). record_type defaults to A."
    ),
    auto_confirm=True,
    timeout_s=20,
)
async def dns_lookup(name: str, record_type: str = "A") -> dict[str, Any]:
    """Resolve DNS records for `name`.

    name: The domain or host to look up (e.g. example.com).
    record_type: A, AAAA, MX, NS, TXT, CNAME, SOA, or PTR (default A).
    """
    name = (name or "").strip()
    rtype = (record_type or "A").strip().upper()
    if not name:
        return {"error": "no name to look up"}
    if rtype not in _DNS_TYPES:
        return {"error": f"unsupported record_type {record_type!r}; "
                         f"use one of {', '.join(_DNS_TYPES)}"}
    dig = shutil.which("dig")
    if dig:
        rc, out, err = await _run([dig, "+short", name, rtype], timeout=15)
        if rc != 0 and not out:
            return {"error": f"dig failed: {err.strip()[:200]}", "name": name, "type": rtype}
        records = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return {"name": name, "type": rtype, "records": records}
    # Fallback when dig is absent (e.g. CI): stdlib resolves A/AAAA only.
    if rtype in ("A", "AAAA"):
        fam = socket.AF_INET if rtype == "A" else socket.AF_INET6
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, name, None, fam)
        except OSError as e:
            return {"error": f"resolve failed: {e}", "name": name, "type": rtype}
        records = sorted({i[4][0] for i in infos})
        return {"name": name, "type": rtype, "records": records,
                "note": "dig not installed — stdlib A/AAAA only"}
    return {"error": f"'dig' is not installed — only A/AAAA resolvable here, not {rtype}"}


@skill(
    name="whois",
    description=(
        "WHOIS a domain or IP — registrar, registration/expiry dates, name servers, org. "
        "For recon/attribution. Returns parsed key fields plus the raw record."
    ),
    auto_confirm=True,
    timeout_s=25,
)
async def whois(query: str) -> dict[str, Any]:
    """WHOIS a domain or IP.

    query: The domain or IP address to look up.
    """
    query = (query or "").strip()
    if not query:
        return {"error": "no domain/IP to look up"}
    binary = shutil.which("whois")
    if not binary:
        return {"error": "'whois' is not installed"}
    rc, out, err = await _run([binary, query], timeout=20)
    if rc != 0 and not out:
        return {"error": f"whois failed: {err.strip()[:200]}", "query": query}
    fields: dict[str, str] = {}
    for key in ("Registrar", "Creation Date", "Registry Expiry Date", "Updated Date",
                "Name Server", "Registrant Organization", "OrgName", "Country"):
        m = re.search(rf"(?im)^\s*{re.escape(key)}\s*:\s*(.+)$", out)
        if m:
            fields[key] = m.group(1).strip()
    return {"query": query, "fields": fields, "raw": out[:4000]}


@skill(
    name="find_subdomains",
    description=(
        "Discover a domain's subdomains with subfinder — PASSIVE OSINT (queries public sources "
        "like certificate-transparency logs; sends NO traffic to the target). The #1 bounty recon "
        "move: surfacing forgotten assets where the vulnerabilities (and the bounties) hide. "
        "Returns the subdomain list; then probe the live ones with port_scan / nuclei_scan. "
        "Authorized programs only."
    ),
    auto_confirm=True,  # passive OSINT, no traffic to the target — like dns_lookup / whois
    timeout_s=130,
)
async def find_subdomains(domain: str) -> dict[str, Any]:
    """Enumerate a domain's subdomains (passive, via subfinder).

    domain: The apex domain (example.com); a URL/host[:port] is reduced to its bare domain.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return {"error": "no domain to enumerate"}
    domain = domain.split("://")[-1].split("/")[0].split(":")[0]  # URL/host → bare domain
    sf = shutil.which("subfinder")
    if not sf:
        return {"error": "'subfinder' is not installed"}
    rc, out, err = await _run([sf, "-d", domain, "-silent"], timeout=120)
    subs = sorted({s.strip().lower() for s in (out or "").splitlines()
                   if s.strip() and "." in s and " " not in s.strip()})
    result: dict[str, Any] = {"domain": domain, "count": len(subs), "subdomains": subs}
    if not subs and (err or "").strip():
        result["note"] = err.strip()[:160]
    return result


def _http_targets(raw: str) -> list[str]:
    """Split a targets string (comma/space/newline-separated hosts or URLs) → cleaned, deduped,
    capped so a probe can't run away."""
    out: list[str] = []
    seen: set[str] = set()
    for p in re.split(r"[\s,]+", (raw or "").strip()):
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out[:1000]


@skill(
    name="http_probe",
    description=(
        "Probe hosts/URLs for LIVE web servers with httpx (ProjectDiscovery) — the recon "
        "triage step after find_subdomains: which hosts serve HTTP, plus status code, page "
        "title, web server, detected tech, and any redirect. targets = a host/URL or a list "
        "(comma/space/newline-separated). Chains find_subdomains → http_probe → nmap_scan / "
        "nuclei_scan on the live ones. ACTIVE — sends HTTP requests. Authorized targets only."
    ),
    auto_confirm=False,  # active HTTP probing of (possibly many) targets — gated
    timeout_s=180,
)
async def http_probe(targets: str) -> dict[str, Any]:
    """Probe hosts/URLs for live web servers + their fingerprint (via httpx).

    targets: one host/URL, or a list separated by commas / spaces / newlines.
    """
    hosts = _http_targets(targets)
    if not hosts:
        return {"error": "no targets to probe"}
    # ProjectDiscovery's httpx is 'httpx-toolkit' on Kali (plain 'httpx' = the python client).
    binary = shutil.which("httpx-toolkit") or shutil.which("httpx")
    if not binary:
        return {"error": "'httpx-toolkit' (ProjectDiscovery httpx) is not installed"}
    fd, listfile = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(hosts) + "\n")
        cmd = [binary, "-l", listfile, "-silent", "-json", "-status-code", "-content-length",
               "-title", "-tech-detect", "-web-server", "-location", "-timeout", "10", "-t", "50"]
        rc, out, err = await _run(cmd, timeout=170)
    finally:
        try:
            os.unlink(listfile)
        except OSError:
            pass
    if not out.strip():
        why = err.strip()[:200] or ("timed out" if rc == -1 else "no live hosts")
        return {"probed": len(hosts), "live": 0, "results": [], "note": why}
    results: list[dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        results.append({
            "url": d.get("url", ""), "status": d.get("status_code"),
            "title": d.get("title", ""), "webserver": d.get("webserver", ""),
            "tech": d.get("tech", []), "content_length": d.get("content_length"),
            "ip": d.get("host_ip", ""), "location": d.get("location", ""),
        })
    return {
        "probed": len(hosts), "live": len(results), "results": results,
        "note": (f"{len(results)}/{len(hosts)} host(s) serve HTTP — point nmap_scan / nuclei_scan "
                 "at the live ones."),
    }


@skill(
    name="searchsploit",
    description=(
        "Search Exploit-DB (offline, via searchsploit) for public exploits matching a product/term "
        "— e.g. 'vsftpd 2.3.4', 'apache struts'. Maps a service+version to known exploits."
    ),
    auto_confirm=True,
    timeout_s=25,
)
async def searchsploit(term: str) -> dict[str, Any]:
    """Search Exploit-DB for `term`.

    term: Product, version, or keywords to search (e.g. "vsftpd 2.3.4").
    """
    term = (term or "").strip()
    if not term:
        return {"error": "no search term"}
    binary = shutil.which("searchsploit")
    if not binary:
        return {"error": "'searchsploit' is not installed (part of the exploitdb package)"}
    rc, out, err = await _run([binary, "-j", *term.split()], timeout=20)
    if not out:
        return {"error": f"searchsploit failed: {err.strip()[:200]}", "term": term}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"term": term, "raw": out[:3000]}
    results = [
        {"title": e.get("Title"), "edb_id": e.get("EDB-ID"), "path": e.get("Path")}
        for e in data.get("RESULTS_EXPLOIT", [])
    ][:25]
    return {"term": term, "count": len(results), "results": results}


# A small common-ports set for the default 'top' scan — the services worth a first look.
_TOP_PORTS = (21, 22, 23, 25, 53, 80, 110, 111, 135, 139, 143, 443, 445, 993, 995,
              1723, 3306, 3389, 5432, 5900, 8080, 8443)


def _parse_ports(ports: str) -> list[int]:
    """Parse 'top', a list '22,80,443', or a range '1-1024' into sorted port numbers (capped)."""
    ports = (ports or "top").strip().lower()
    if ports in ("", "top"):
        return list(_TOP_PORTS)
    out: set[int] = set()
    for part in ports.split(","):
        part = part.strip()
        if "-" in part:
            lo, _, hi = part.partition("-")
            if lo.isdigit() and hi.isdigit():
                out.update(range(int(lo), min(int(hi), 65535) + 1))
        elif part.isdigit():
            out.add(int(part))
    return sorted(p for p in out if 0 < p <= 65535)[:1024]  # bounded so a scan can't run away


@skill(
    name="port_scan",
    description=(
        "TCP port-scan a host (pure connect scan — no root). ports = 'top' (common ports), a list "
        "like '22,80,443', or a range '1-1024'. Returns OPEN ports + a banner where offered. "
        "Fast triage; use nmap via run_command for service/version detection."
    ),
    auto_confirm=False,  # active network probe — gated like run_command
    timeout_s=45,
)
async def port_scan(host: str, ports: str = "top") -> dict[str, Any]:
    """Scan `host` for open TCP ports.

    host: Target hostname or IP (authorized targets only).
    ports: 'top', a comma list (22,80,443), or a range (1-1024).
    """
    host = (host or "").strip()
    if not host:
        return {"error": "no host to scan"}
    targets = _parse_ports(ports)
    if not targets:
        return {"error": f"no valid ports parsed from {ports!r}"}
    sem = asyncio.Semaphore(200)  # bound concurrency so a big range stays sane

    async def _probe(port: int) -> tuple[int, str] | None:
        async with sem:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=1.5
                )
            except (OSError, TimeoutError):
                return None
            banner = ""
            try:
                data = await asyncio.wait_for(reader.read(80), timeout=0.5)
                banner = data.decode("utf-8", "replace").strip()
            except (OSError, TimeoutError):
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return port, banner

    found = await asyncio.gather(*[_probe(p) for p in targets])
    open_ports = [{"port": r[0], "banner": r[1]} for r in found if r]
    return {"host": host, "scanned": len(targets), "open": open_ports}


def _local_subnet_24() -> str | None:
    """This machine's local /24 (e.g. 192.168.2.0/24), from the default-route source IP. No packets
    are sent — connecting a UDP socket only selects the source address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    parts = ip.split(".")
    return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24" if len(parts) == 4 else None


def _hosts_from_cidr(cidr: str) -> list[str]:
    """Host IPs in a CIDR, capped to a /24 so a sweep can't run away across a huge range."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return []
    if net.num_addresses > 256:
        return []
    return [str(h) for h in net.hosts()] or [str(net.network_address)]


@skill(
    name="discover_hosts",
    description=(
        "Find live hosts on a local subnet via a fast async TCP-connect sweep on common ports (no "
        "root, no nmap). subnet = a CIDR like 192.168.1.0/24 (blank = auto-detect this machine's "
        "/24). Returns reachable hosts + which probed ports answered. Authorized networks only."
    ),
    auto_confirm=False,  # active network sweep — gated like port_scan
    timeout_s=60,
)
async def discover_hosts(subnet: str = "",
                         ports: str = "22,80,443,445,3389,53,8080") -> dict[str, Any]:
    """Discover live hosts on a subnet — the recon step before port_scan.

    subnet: CIDR (192.168.1.0/24). Blank = auto-detect this host's /24.
    ports: TCP ports to probe per host; a host answering ANY of them is reported live.
    """
    subnet = (subnet or "").strip() or _local_subnet_24() or ""
    if not subnet:
        return {"error": "could not determine a subnet — pass one like 192.168.1.0/24"}
    hosts = _hosts_from_cidr(subnet)
    if not hosts:
        return {"error": f"invalid or too-large subnet {subnet!r} (max /24)"}
    probe_ports = _parse_ports(ports)
    if not probe_ports:
        return {"error": f"no valid ports parsed from {ports!r}"}
    sem = asyncio.Semaphore(256)  # bound total concurrent sockets

    async def _probe(host: str, port: int, found: list[int]) -> None:
        async with sem:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=1.0)
            except (OSError, TimeoutError):
                return
            found.append(port)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _scan_host(host: str) -> dict[str, Any] | None:
        found: list[int] = []
        await asyncio.gather(*[_probe(host, p, found) for p in probe_ports])
        return {"host": host, "ports": sorted(found)} if found else None

    results = await asyncio.gather(*[_scan_host(h) for h in hosts])
    live = [r for r in results if r]
    return {"subnet": subnet, "scanned": len(hosts), "live": live}


def _nmap_ports_ok(ports: str) -> bool:
    """A safe nmap port spec: 'top', or digits/commas/hyphens only (blocks flag/shell injection)."""
    return ports.lower() == "top" or bool(re.fullmatch(r"[0-9][0-9,\-]*", ports))


def _parse_nmap_xml(xml_text: str) -> list[dict[str, Any]]:
    """Parse nmap -oX output into hosts → open ports with service/version + NSE script output."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)  # nmap's own well-formed output; no external entities
    except ET.ParseError:
        return []
    hosts: list[dict[str, Any]] = []
    for host in root.findall("host"):
        status = host.find("status")
        if status is not None and status.get("state") == "down":
            continue
        addr = next((a.get("addr", "") for a in host.findall("address")
                     if a.get("addrtype") in ("ipv4", "ipv6")), "")
        hn = host.find("hostnames/hostname")
        hostname = hn.get("name", "") if hn is not None else ""
        open_ports: list[dict[str, Any]] = []
        for port in host.findall("ports/port"):
            state = port.find("state")
            if state is None or state.get("state") != "open":
                continue
            svc = port.find("service")
            open_ports.append({
                "port": int(port.get("portid", "0") or 0),
                "protocol": port.get("protocol", "tcp"),
                "service": svc.get("name", "") if svc is not None else "",
                "product": svc.get("product", "") if svc is not None else "",
                "version": svc.get("version", "") if svc is not None else "",
                "scripts": [{"id": s.get("id", ""), "output": (s.get("output", "") or "").strip()}
                            for s in port.findall("script")],
            })
        if addr or open_ports:
            hosts.append({"ip": addr, "hostname": hostname, "open_ports": open_ports})
    return hosts


@skill(
    name="nmap_scan",
    description=(
        "Scan a host with nmap for open ports + SERVICE/VERSION detection (-sV) — the real recon "
        "workhorse that upgrades port_scan's open/closed triage with what's actually running (e.g. "
        "'nginx 1.18', 'OpenSSH 8.9'), so you can chain searchsploit / nuclei_scan for known-vuln "
        "matches. ports = 'top' (nmap's top 1000) or a spec like '22,80,443' / '1-1000'. Set "
        "scripts=true to also run nmap's default NSE scripts (-sC: titles, banners, vuln checks) "
        "— any VULNERABLE hit is surfaced as a finding. Runs without root (TCP connect scan). "
        "ACTIVE — sends packets. Authorized targets only."
    ),
    auto_confirm=False,  # active network scan — gated like port_scan / run_command
    timeout_s=300,
)
async def nmap_scan(target: str, ports: str = "top", scripts: str = "") -> dict[str, Any]:
    """Nmap service/version scan of a host.

    target:  Host, IP, or CIDR (authorized targets only).
    ports:   'top' (nmap's top-1000) or a spec like '22,80,443' or '1-1000'.
    scripts: 'true' also runs nmap's default NSE scripts (-sC) — more detail + some vuln checks.
    """
    target = (target or "").strip()
    if not target:
        return {"error": "no target to scan"}
    if target.startswith("-"):
        return {"error": f"invalid target {target!r}"}  # never let a target be read as an nmap flag
    ports = (ports or "top").strip()
    if not _nmap_ports_ok(ports):
        return {"error": f"invalid port spec {ports!r} — use 'top', '22,80,443', or '1-1000'"}
    nmap = shutil.which("nmap")
    if not nmap:
        return {"error": "'nmap' is not installed"}
    cmd = [nmap, "-sV", "-Pn", "-T4", "-oX", "-"]
    if str(scripts).strip().lower() in ("1", "true", "yes", "on"):
        cmd.append("-sC")
    if ports.lower() != "top":
        cmd += ["-p", ports]
    cmd.append(target)
    rc, out, err = await _run(cmd, timeout=290)
    if not out.strip():
        why = err.strip()[:200] or ("timed out" if rc == -1 else "nmap returned nothing")
        return {"error": f"nmap failed: {why}", "target": target}
    hosts = _parse_nmap_xml(out)
    open_count = sum(len(h["open_ports"]) for h in hosts)
    findings = []  # surface any NSE script that reports a vulnerability (common finding schema)
    for h in hosts:
        for p in h["open_ports"]:
            for sc in p["scripts"]:
                if "VULNERABLE" in sc["output"] or "vuln" in sc["id"].lower():
                    findings.append({
                        "id": f"nmap-{sc['id']}-{h['ip']}-{p['port']}",
                        "name": f"nmap NSE {sc['id']} on {p['service'] or p['port']}",
                        "severity": "medium",
                        "matched_at": f"{h['ip']}:{p['port']}",
                        "description": sc["output"][:500],
                    })
    result: dict[str, Any] = {
        "target": target, "hosts": hosts, "open_ports": open_count,
        "note": (f"{open_count} open port(s) across {len(hosts)} host(s) — feed each "
                 "service+version into searchsploit / nuclei_scan for known-vuln matches."),
    }
    if findings:
        result["findings"] = findings
    return result


# The full rockyou (14M) is the CTF standard — prefer it uncompressed if it exists.
_CRACK_WORDLISTS = (
    "/usr/share/wordlists/rockyou.txt",
)
# Kali ships rockyou GZIPPED, so the full list is present but John can't read it directly. Extract
# it once into a cache and reuse it — this beats the smaller subsets below, which only have a tiny
# fraction of the passwords (live-found: the seclists rockyou-75 subset is ~59k vs rockyou's 14M).
_GZ_WORDLISTS = ("/usr/share/wordlists/rockyou.txt.gz",)
_WORDLIST_CACHE = Path.home() / ".cache" / "friday" / "rockyou.txt"
# Fallbacks if the full rockyou is unavailable: the curated subset, then a weak web-dir list.
_FALLBACK_WORDLISTS = (
    "/usr/share/seclists/Passwords/Leaked-Databases/rockyou-75.txt",
    "/usr/share/wordlists/dirb/common.txt",
)


def _extract_gzipped_wordlist() -> str | None:
    """Kali ships rockyou as rockyou.txt.gz; John needs it uncompressed. Extract the full list
    once into the cache (atomic write) and return that path. Best-effort: None if it can't."""
    if _WORDLIST_CACHE.is_file() and _WORDLIST_CACHE.stat().st_size > 0:
        return str(_WORDLIST_CACHE)  # already extracted on a prior run
    for gz in _GZ_WORDLISTS:
        if not Path(gz).is_file():
            continue
        try:
            _WORDLIST_CACHE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _WORDLIST_CACHE.with_suffix(".tmp")
            with gzip.open(gz, "rt", errors="ignore") as src, open(tmp, "w") as dst:
                shutil.copyfileobj(src, dst)
            tmp.replace(_WORDLIST_CACHE)  # atomic — never leaves a half-written cache
            return str(_WORDLIST_CACHE)
        except OSError:
            Path(_WORDLIST_CACHE).with_suffix(".tmp").unlink(missing_ok=True)
    return None


def _default_crack_wordlist() -> str | None:
    """Best available password wordlist: a real uncompressed list, else the gzipped rockyou
    extracted to cache (the common Kali case), else a weak last-ditch fallback."""
    for w in _CRACK_WORDLISTS:
        if Path(w).is_file():
            return w
    extracted = _extract_gzipped_wordlist()
    if extracted:
        return extracted
    return next((w for w in _FALLBACK_WORDLISTS if Path(w).is_file()), None)


def _parse_john_show(out: str) -> str | None:
    """Pull the password out of `john --show` output — '?:password' / 'user:password' lines (the
    'N password hashes cracked' summary line is skipped)."""
    for line in out.splitlines():
        line = line.strip()
        if not line or "password hash" in line.lower():
            continue
        if ":" in line:
            pw = line.split(":", 1)[1]
            if pw:
                return pw
    return None


@skill(
    name="crack_hash",
    description=(
        "Crack a password hash with John the Ripper against a wordlist (rockyou by default). For "
        "CTF loot — a hash you recovered from your own box. hash_format (e.g. raw-md5, raw-sha256) "
        "is optional; John auto-detects. Set rules=true to also try mangled variants (capitalize, "
        "leetspeak, append digits/years) — needed for tougher passwords. Returns the password if "
        "found. Authorized targets only."
    ),
    auto_confirm=False,  # intensive offline crack — gated
    timeout_s=140,
)
async def crack_hash(hash_value: str, hash_format: str = "", wordlist: str = "",
                     rules: str = "") -> dict[str, Any]:
    """Crack a single password hash with John the Ripper.

    hash_value: The hash string to crack.
    hash_format: Optional John format (raw-md5, raw-sha256, nt, …); auto-detected if blank.
    wordlist: Path to a wordlist; defaults to rockyou / a seclists subset if present.
    rules: Mangling rules for tougher passwords — "" off; "true"/"wordlist" for John's default
        wordlist rules (capitalize, leetspeak, append digits/years); or a named set (e.g. Jumbo).
    """
    hash_value = (hash_value or "").strip()
    if not hash_value:
        return {"error": "no hash to crack"}
    john = shutil.which("john")
    if not john:
        return {"error": "'john' (John the Ripper) is not installed"}
    wl = (wordlist or "").strip() or _default_crack_wordlist()
    if not wl or not Path(wl).is_file():
        return {"error": "no wordlist found — install rockyou/seclists or pass wordlist=<path>"}
    fmt = (hash_format or "").strip()
    rules = (rules or "").strip()
    rule_args = []
    if rules:
        rule_args = (["--rules"] if rules.lower() in ("1", "true", "yes", "on", "wordlist",
                     "default") else [f"--rules={rules}"])
    fd, hashfile = tempfile.mkstemp(suffix=".hash")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(hash_value + "\n")
        crack = [john, f"--wordlist={wl}", *rule_args,
                 *([f"--format={fmt}"] if fmt else []), hashfile]
        rc, _out, _err = await _run(crack, timeout=135)
        show = [john, "--show", *([f"--format={fmt}"] if fmt else []), hashfile]
        _, show_out, _ = await _run(show, timeout=15)
        password = _parse_john_show(show_out)
        result: dict[str, Any] = {
            "hash": hash_value, "cracked": bool(password), "tool": "john",
            "format": fmt or "(auto-detected)", "rules": rules or "(none)",
        }
        if password:
            result["password"] = password
        elif rc == -1:
            result["note"] = "timed out before cracking — try a targeted wordlist or hash_format"
        return result
    finally:
        try:
            os.unlink(hashfile)
        except OSError:
            pass


def _looks_like_url(u: str) -> bool:
    """A target must be a real http(s) URL — so it can't be read as an sqlmap flag or local path."""
    return bool(re.match(r"https?://", u, re.I))


def _parse_sqlmap(out: str) -> dict[str, Any]:
    """Parse sqlmap --batch stdout into a detection-only report: which parameters are injectable,
    the injection type(s), and the back-end DBMS. Reads ONLY the injection-point block sqlmap prints
    when it confirms a vuln — never any dumped data (the skill never asks sqlmap to dump)."""
    injectable = False
    params: list[dict[str, Any]] = []
    dbms = ""
    m = re.search(r"back-end DBMS:\s*(.+)", out)  # e.g. "back-end DBMS: MySQL >= 5.0.12"
    if m:
        dbms = m.group(1).strip()
    cur: dict[str, Any] | None = None
    for line in out.splitlines():
        pm = re.match(r"\s*Parameter:\s*(.+?)\s*\((\w+)\)\s*$", line)  # "Parameter: cat (GET)"
        if pm:
            injectable = True
            cur = {"parameter": pm.group(1).strip(), "place": pm.group(2).strip(), "types": []}
            params.append(cur)
            continue
        tm = re.match(r"\s*Type:\s*(.+)", line)  # "    Type: boolean-based blind"
        if tm and cur is not None:
            cur["types"].append(tm.group(1).strip())
    return {"injectable": injectable, "parameters": params, "dbms": dbms}


@skill(
    name="sql_injection_test",
    description=(
        "Test a URL for SQL injection with sqlmap — CONFIRMS whether a parameter is injectable and "
        "reports the injection type(s) + back-end DBMS. A top bug-bounty class. Give a full URL "
        "with a query string (e.g. 'http://site/item.php?id=1'); optional data=<POST body> tests "
        "params, param=<name> focuses one parameter, level (1-5) / risk (1-3) tune test depth. "
        "SAFETY: this only CONFIRMS the injection — it NEVER dumps databases, reads tables, runs a "
        "shell, or exfiltrates data (a human decides any of that, separately). Bounded, "
        "non-interactive (--batch), fresh test each run. ACTIVE exploitation — authorized only."
    ),
    auto_confirm=False,  # active SQLi exploitation — always gated
    timeout_s=600,
)
async def sql_injection_test(
    target: str, data: str = "", param: str = "", level: int = 1, risk: int = 1
) -> dict[str, Any]:
    """Confirm SQL injection on a URL with sqlmap (DETECTION ONLY — never dumps data).

    target: full URL with a query string, e.g. 'http://site/item.php?id=1'.
    data:   optional POST body to test POST parameters (e.g. 'user=1&pass=2').
    param:  optional single parameter name to focus testing on (-p).
    level:  sqlmap test depth 1-5 (default 1). risk: 1-3 (default 1). Both bounded.
    """
    target = (target or "").strip()
    if not target:
        return {"error": "no target URL to test"}
    if target.startswith("-") or not _looks_like_url(target):
        return {"error": f"invalid target {target!r} — give a full http(s):// URL with a query "
                         "string, e.g. 'http://site/item.php?id=1'"}
    sqlmap = shutil.which("sqlmap")
    if not sqlmap:
        return {"error": "'sqlmap' is not installed"}
    lvl = max(1, min(5, int(level) if str(level).strip().lstrip("-").isdigit() else 1))
    rsk = max(1, min(3, int(risk) if str(risk).strip().lstrip("-").isdigit() else 1))
    # --flush-session forces a genuine fresh test each run (never report a cached positive);
    # deliberately NO --dump / --dbs / --tables / --os-shell — CONFIRMS injection, nothing more.
    cmd = [sqlmap, "-u", target, "--batch", "--disable-coloring", "--flush-session",
           f"--level={lvl}", f"--risk={rsk}"]
    if data.strip():
        cmd += ["--data", data.strip()]
    if param.strip():
        if param.strip().startswith("-"):
            return {"error": f"invalid parameter {param!r}"}
        cmd += ["-p", param.strip()]
    rc, out, err = await _run(cmd, timeout=590)
    if not out.strip():
        why = err.strip()[:200] or ("timed out" if rc == -1 else "sqlmap returned nothing")
        return {"error": f"sqlmap failed: {why}", "target": target}
    parsed = _parse_sqlmap(out)
    result: dict[str, Any] = {
        "target": target, "injectable": parsed["injectable"],
        "dbms": parsed["dbms"], "parameters": parsed["parameters"],
    }
    if parsed["injectable"]:
        result["findings"] = [{
            "id": f"sqli-{target}-{p['parameter']}",
            "name": f"SQL injection in '{p['parameter']}' ({p['place']})",
            "severity": "high",
            "matched_at": target,
            "description": (f"Parameter '{p['parameter']}' is injectable via: "
                            f"{', '.join(p['types']) or 'SQL injection'}."
                            + (f" Back-end DBMS: {parsed['dbms']}." if parsed["dbms"] else "")),
        } for p in parsed["parameters"]]
        result["note"] = (f"CONFIRMED SQLi in {len(parsed['parameters'])} parameter(s). Detection "
                          "only — no data was dumped; the operator decides further exploitation.")
    else:
        result["note"] = "No SQL injection confirmed at this level/risk (try raising level / risk)."
    return result
