"""Exposed-secret scanning — hardcoded keys/tokens in a page and its JS bundles.

scan_secrets fetches a URL and (if it's HTML) the JavaScript files it loads, then greps the content
for high-signal secrets: AWS/Google/GitHub/Stripe/Slack/SendGrid keys, private-key blocks, Slack
webhooks, JWTs, and lower-confidence generic `api_key=...` assignments. Front-end bundles routinely
leak real credentials a templated scanner never looks at — a genuinely payable, scanner-blind find.
PASSIVE: it only GETs what a browser loads (bounded), sends no attacks. Pure-stdlib (urllib/re),
CI-safe, $0. Findings are CANDIDATES — verify the secret is live and in scope before reporting.
"""

from __future__ import annotations

import asyncio
import http.client
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin

from friday.skills.registry import skill

# (id, compiled pattern, severity) — high-signal first. Tuned to avoid the obvious false positives;
# the generic assignment catch below is deliberately separate and low-confidence.
_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("aws-access-key-id", re.compile(r"AKIA[0-9A-Z]{16}"), "high"),
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "high"),
    ("github-token", re.compile(r"gh[opsur]_[0-9A-Za-z]{36,}"), "high"),
    ("github-pat", re.compile(r"github_pat_[0-9A-Za-z_]{82}"), "high"),
    ("slack-token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"), "high"),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"), "high"),
    ("stripe-secret-key", re.compile(r"sk_live_[0-9A-Za-z]{24,}"), "critical"),
    ("stripe-restricted-key", re.compile(r"rk_live_[0-9A-Za-z]{24,}"), "high"),
    ("sendgrid-key", re.compile(r"SG\.[\w-]{22}\.[\w-]{43}"), "high"),
    (
        "private-key-block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
        "critical",
    ),
    ("twilio-account-sid", re.compile(r"AC[0-9a-fA-F]{32}"), "medium"),
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        "medium",
    ),
    (
        "google-oauth-client",
        re.compile(r"[0-9]+-[0-9A-Za-z_]{32}\.apps\.googleusercontent\.com"),
        "low",
    ),
]

# Lower-confidence: `api_key = "..."` / `"secret": "..."` style assignments (lots of false hits).
_GENERIC = re.compile(
    r"""(?i)['"]?(api[_-]?key|secret|secret[_-]?key|access[_-]?token|"""
    r"""auth[_-]?token|client[_-]?secret|"""
    r"""password|passwd|private[_-]?key)['"]?\s*[:=]\s*['"]([^'"\s]{8,64})['"]"""
)

_SCRIPT_SRC = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)
_SEV = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _fetch(url: str) -> tuple[int, str, str, str | None]:
    """(status, body, content_type, error). Transport failure → error string; HTTP errors return the
    body too (an error page can still leak a key)."""
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-secret-scan/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:  # noqa: S310 — authorized target
            return (
                r.status,
                r.read(3_000_000).decode("utf-8", "replace"),
                r.headers.get_content_type(),
                None,
            )
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(1_000_000).decode("utf-8", "replace"), "", None
        except Exception:  # noqa: BLE001
            return e.code, "", "", None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        return 0, "", "", str(e)[:160]


def _scan(text: str, source: str) -> list[dict[str, Any]]:
    """Run every pattern over one document; return raw findings tagged with the source."""
    out: list[dict[str, Any]] = []
    for fid, pat, sev in _PATTERNS:
        for m in pat.finditer(text):
            out.append(
                {
                    "id": f"exposed-{fid}",
                    "kind": fid,
                    "severity": sev,
                    "secret": m.group(0)[:80],
                    "source": source,
                }
            )
    for m in _GENERIC.finditer(text):
        out.append(
            {
                "id": "exposed-generic-secret",
                "kind": f"generic:{m.group(1).lower()}",
                "severity": "low",
                "secret": m.group(2)[:80],
                "source": source,
            }
        )
    return out


@skill(
    name="scan_secrets",
    description=(
        "Scan a web page AND the JavaScript bundles it loads for hardcoded secrets — AWS/Google/"
        "GitHub/Stripe/Slack/SendGrid keys, private-key blocks, Slack webhooks, JWTs, and generic "
        "api_key/secret assignments. Front-end bundles leak real credentials a templated scanner "
        "never checks — a payable, scanner-blind find. Returns the matches with the source URL and "
        "severity. Passive (GETs what a browser loads, bounded by max_js). Verify the secret is "
        "live and in scope before reporting."
    ),
    auto_confirm=True,  # passive: GETs the page + its JS, like viewing source; no attacks
    timeout_s=60,
)
async def scan_secrets(url: str, max_js: int = 10) -> dict[str, Any]:
    """Fetch a URL (and its linked JS) and report any hardcoded secrets found.

    url: the page or JS file to scan (a bare host defaults to http://).
    max_js: how many linked JavaScript files to also fetch and scan (default 10).
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to scan"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    def _run() -> tuple[list[dict[str, Any]], list[str], str | None]:
        status, body, ctype, err = _fetch(url)
        if err:
            return [], [], err
        raw = _scan(body, url)
        scanned = [url]
        if "html" in ctype or body.lstrip()[:1] == "<":
            js_urls: list[str] = []
            for src in _SCRIPT_SRC.findall(body):
                full = urljoin(url, src)
                if full.split("?")[0].endswith(".js") and full not in js_urls:
                    js_urls.append(full)
            for ju in js_urls[: max(0, max_js)]:
                st, jb, _, jerr = _fetch(ju)
                if not jerr and jb:
                    raw += _scan(jb, ju)
                    scanned.append(ju)
        return raw, scanned, None

    raw, scanned, err = await asyncio.to_thread(_run)
    if err:
        return {"error": f"could not fetch {url}: {err}", "url": url}

    # Dedup by (kind, secret) so a key found in two files is one finding listing both sources.
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for f in raw:
        key = (f["kind"], f["secret"])
        g = merged.get(key)
        if g is None:
            g = {
                "id": f["id"],
                "kind": f["kind"],
                "severity": f["severity"],
                "secret": f["secret"],
                "sources": [],
                "name": f"Exposed secret: {f['kind']}",
            }
            merged[key] = g
        if f["source"] not in g["sources"]:
            g["sources"].append(f["source"])
    findings = sorted(merged.values(), key=lambda f: _SEV.get(f["severity"], 9))
    for f in findings:  # report-grade: matched_at + a verify-first description
        f["matched_at"] = f["sources"][0]
        f["description"] = (
            f"A {f['kind']} secret was found in {', '.join(f['sources'])}. Verify it "
            "is live and in scope; a redacted/example/expired key is not a finding."
        )

    return {
        "url": url,
        "scanned": scanned,
        "count": len(findings),
        "findings": findings,
        "vulnerable": any(f["severity"] in ("critical", "high") for f in findings),
        "note": (
            "Candidates only — confirm each secret is REAL and live (it grants access) and the "
            "asset is in scope before reporting; bundles are full of test/placeholder keys."
        ),
    }
