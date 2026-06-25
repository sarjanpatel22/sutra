"""Web-phase recon skills — the step AFTER port_scan finds a web server (80/443/8080):

- web_enum: content/directory or vhost enumeration (gobuster, ffuf fallback) — the #1 next move on
  a TryHackMe web box, returned as a structured list the agent can reason over. It is an ACTIVE
  brute (many requests) so it is gated (auto_confirm=False), like port_scan and run_command.
- http_headers: a single passive GET that returns the response headers and flags MISSING security
  headers (CSP/HSTS/X-Frame-Options/…) — pure-stdlib (urllib), works offline/in CI, auto-approved.

The binary-backed web_enum degrades gracefully to a clear "not installed" error, so a clean CI
install never breaks (gobuster/ffuf live on the Kali box, not CI). Authorized targets only.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from friday.skills.recon import _run  # shared subprocess helper (returncode, stdout, stderr)
from friday.skills.registry import skill

# Default wordlists, in order of preference — both ship on Kali; a clean CI box has neither.
_WORDLISTS = (
    "/usr/share/seclists/Discovery/Web-Content/common.txt",
    "/usr/share/wordlists/dirb/common.txt",
)
# gobuster -q line: "/admin (Status: 301) [Size: 312]" — though some builds drop the leading "/"
# ("admin   (Status: 200) [Size: 98730]"), so the path token is matched with or without it.
_GOBUSTER_LINE = re.compile(r"(?m)^(\S+)\s+\(Status:\s*(\d+)\)(?:\s+\[Size:\s*(\d+)\])?")


def _default_wordlist() -> str | None:
    return next((w for w in _WORDLISTS if Path(w).is_file()), None)


async def _wildcard_len(url: str) -> int | None:
    """Probe a random path: if a SPA/catch-all answers 200 (every route returns the app shell),
    return its body length so brute-force can EXCLUDE that length — otherwise every word is a false
    200 and gobuster aborts. Returns None when a missing path 404s normally (no catch-all)."""
    probe = url.rstrip("/") + "/zz" + os.urandom(6).hex()

    def _get() -> int | None:
        try:
            with urllib.request.urlopen(probe, timeout=8) as r:  # noqa: S310
                return len(r.read()) if r.status == 200 else None
        except (urllib.error.HTTPError, OSError):
            return None

    return await asyncio.to_thread(_get)


@skill(
    name="web_enum",
    description=(
        "Enumerate a web server's hidden content — directories/files (mode='dirs') or vhosts "
        "(mode='vhost') — via gobuster (ffuf fallback). The standard next move once a web port is "
        "open. Returns the discovered paths with status codes. Active brute-force, authorized only."
    ),
    auto_confirm=False,  # active brute-force (many requests) — gated like port_scan
    timeout_s=120,
)
async def web_enum(url: str, mode: str = "dirs", wordlist: str = "") -> dict[str, Any]:
    """Brute-force a web server's content or vhosts.

    url: Target base URL (http(s)://host[:port]); a bare host defaults to http://.
    mode: 'dirs' (directories/files, default) or 'vhost' (virtual hosts).
    wordlist: Path to a wordlist; defaults to seclists/dirb common.txt if present.
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to enumerate"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    mode = (mode or "dirs").strip().lower()
    if mode not in ("dirs", "vhost"):
        return {"error": "mode must be 'dirs' or 'vhost'"}
    wl = (wordlist or "").strip() or _default_wordlist()
    if not wl or not Path(wl).is_file():
        return {"error": "no wordlist found — install seclists/dirb or pass wordlist=<path>"}

    gobuster = shutil.which("gobuster")
    if gobuster:
        sub = "dir" if mode == "dirs" else "vhost"
        cmd = [gobuster, sub, "-u", url, "-w", wl, "-q", "--no-error", "-t", "40"]
        if mode == "dirs" and (wlen := await _wildcard_len(url)) is not None:
            # SPA / catch-all: exclude the index-shell length so real paths still surface (else
            # every word 200s and gobuster aborts — the exact failure seen against Juice Shop)
            cmd += ["--exclude-length", str(wlen)]
        rc, out, err = await _run(cmd, timeout=115)
        if rc != 0 and not out:
            return {"error": f"gobuster failed: {err.strip()[:200]}", "url": url}
        found = [
            {"path": m.group(1) if m.group(1).startswith("/") else "/" + m.group(1),
             "status": int(m.group(2)),
             "size": int(m.group(3)) if m.group(3) else None}
            for m in _GOBUSTER_LINE.finditer(out)
        ]
        if not found:  # vhost (or odd) output — keep the raw hits so nothing is silently dropped
            found = [{"line": ln.strip()} for ln in out.splitlines() if ln.strip()][:50]
        return {"url": url, "mode": mode, "tool": "gobuster", "count": len(found), "found": found}

    ffuf = shutil.which("ffuf")
    if ffuf:
        rc, out, err = await _run([ffuf, "-u", f"{url}/FUZZ", "-w", wl, "-s"], timeout=115)
        hits = [ln.strip() for ln in out.splitlines() if ln.strip()][:100]
        return {"url": url, "mode": mode, "tool": "ffuf", "count": len(hits),
                "found": [{"path": h} for h in hits]}

    return {"error": "no web-enum tool installed (need gobuster or ffuf)"}


_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "unknown": 5}


def _parse_nuclei(out: str) -> list[dict[str, Any]]:
    """Parse nuclei -jsonl stdout into clean, triageable findings (sorted worst-first). Each line
    is one finding; non-JSON lines (any stray logs) are skipped."""
    findings: list[dict[str, Any]] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        info = d.get("info") or {}
        cls = info.get("classification") or {}
        findings.append({
            "id": d.get("template-id") or d.get("templateID") or "?",
            "name": info.get("name") or "?",
            "severity": str(info.get("severity") or "unknown").lower(),
            "matched_at": d.get("matched-at") or d.get("matched") or d.get("host") or "",
            "tags": info.get("tags") or [],
            # report-grade fields — all real tool output, so a drafted report can't fabricate:
            "description": info.get("description") or "",
            "reference": info.get("reference") or [],
            "cwe": cls.get("cwe-id") or [],
            "cvss": cls.get("cvss-score"),
            "curl": d.get("curl-command") or "",  # ready-to-paste reproduction
        })
    findings.sort(key=lambda f: _SEV_ORDER.get(f["severity"], 9))
    return findings


@skill(
    name="nuclei_scan",
    description=(
        "Scan a web target for KNOWN vulnerabilities, misconfigurations, exposures and CVEs with "
        "nuclei and its community templates — the core bug-bounty FIND step. Returns the real "
        "findings (template id, name, severity, the exact matched URL) as structured data, so they "
        "can be triaged and reported without guessing. severity = comma list to include "
        "(info,low,medium,high,critical; default skips info noise); tags = restrict templates "
        "(e.g. cve,exposure,misconfiguration,takeover). Active scan — AUTHORIZED targets only."
    ),
    auto_confirm=False,  # active vulnerability scan — gated like web_enum / port_scan
    timeout_s=300,
)
async def nuclei_scan(target: str, severity: str = "low,medium,high,critical",
                      tags: str = "") -> dict[str, Any]:
    """Scan a target for known vulnerabilities with nuclei.

    target: URL or host (http(s)://host[:port]); a bare host defaults to http://.
    severity: comma list to include (info,low,medium,high,critical). Default skips info noise.
    tags: optional comma list to restrict the template set (e.g. 'cve,exposure,takeover').
    """
    target = (target or "").strip()
    if not target:
        return {"error": "no target to scan"}
    if not target.startswith(("http://", "https://")):
        target = "http://" + target
    nuclei = shutil.which("nuclei")
    if not nuclei:
        return {"error": "'nuclei' is not installed"}
    # Write findings to a FILE, not just stdout: _run discards stdout on timeout, so a long scan
    # that already found real bugs would lose them. The file keeps whatever nuclei wrote before it
    # was killed, so a partial scan still returns its findings (real money — never thrown away).
    fd, outfile = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    cmd = [nuclei, "-u", target, "-jsonl", "-o", outfile, "-silent", "-disable-update-check",
           "-no-color", "-rate-limit", "60", "-timeout", "8"]
    if (sev := (severity or "").strip()):
        cmd += ["-severity", sev]
    if (tg := (tags or "").strip()):
        cmd += ["-tags", tg]
    try:
        rc, out, err = await _run(cmd, timeout=285)
        try:
            file_out = Path(outfile).read_text()
        except OSError:
            file_out = ""
    finally:
        try:
            os.unlink(outfile)
        except OSError:
            pass
    findings = _parse_nuclei(file_out or out)  # prefer the file; fall back to stdout
    result: dict[str, Any] = {"target": target, "tool": "nuclei",
                              "count": len(findings), "findings": findings}
    if rc == -1:  # timed out — the findings here are PARTIAL, flag it even if some were recovered
        result["incomplete"] = True
        result["note"] = (
            f"scan TIMED OUT — results are partial ({len(findings)} found so far); "
            "re-run (narrow with tags= or a higher severity=) for full coverage")
    elif not findings:
        low = (err or "").lower()
        if "no templates" in low or ("template" in low and "error" in low):
            result["incomplete"] = True  # a real failure, not a clean result
            result["note"] = "no templates loaded — run: nuclei -update-templates"
        else:
            result["note"] = "no findings at this severity (try lower severity or specific tags)"
    return result


_SECURITY_HEADERS = (
    "Content-Security-Policy", "Strict-Transport-Security", "X-Frame-Options",
    "X-Content-Type-Options", "Referrer-Policy",
)


@skill(
    name="http_headers",
    description=(
        "Fetch a URL's HTTP response headers and flag MISSING security headers (CSP, HSTS, "
        "X-Frame-Options, …). Quick passive recon on a web target — reveals the server/tech and "
        "weak headers. A single GET, no brute-force."
    ),
    auto_confirm=True,  # single passive GET, like web_extract
    timeout_s=15,
)
async def http_headers(url: str) -> dict[str, Any]:
    """Return a URL's response headers + which security headers are missing.

    url: The target URL (a bare host defaults to http://).
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    def _fetch() -> tuple[int, dict[str, str]]:
        req = urllib.request.Request(url, headers={"User-Agent": "sutra-recon/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 — recon on an authorized target
                return r.status, dict(r.headers)
        except urllib.error.HTTPError as e:  # 4xx/5xx still carry headers worth seeing
            return e.code, dict(e.headers)

    try:
        status, headers = await asyncio.to_thread(_fetch)
    except Exception as e:  # noqa: BLE001 — unreachable host / bad URL → return, don't raise
        return {"error": f"could not fetch {url}: {e}"}
    missing = [h for h in _SECURITY_HEADERS if h not in headers]
    return {
        "url": url,
        "status": status,
        "server": headers.get("Server"),
        "headers": dict(headers),
        "missing_security_headers": missing,
    }
