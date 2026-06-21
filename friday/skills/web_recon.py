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
import re
import shutil
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
        rc, out, err = await _run(
            [gobuster, sub, "-u", url, "-w", wl, "-q", "--no-error", "-t", "40"], timeout=115
        )
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
