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
import json
import re
import shutil
import socket
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
