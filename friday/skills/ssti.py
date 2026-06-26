"""Server-side template injection (SSTI) probing - a payable class, frequently a path to RCE.

probe_ssti injects template-evaluation payloads (Jinja/Twig/FreeMarker/ERB/Smarty) carrying a unique
multiplication and checks whether the EVALUATED product appears in the response. If 1337*1337 comes
back as 1787569, the server rendered our input as a template - SSTI (often a path to RCE).
Deterministic; pure-stdlib, CI-safe, $0. Active (sends payloads) so it is gated; confirm + escalate.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from friday.skills.registry import skill

_EXPECT = str(1337 * 1337)  # 1787569 - unlikely to occur naturally, so a hit means real evaluation
_PAYLOADS = [
    ("jinja2/twig/nunjucks", "{{1337*1337}}"),
    ("freemarker/jsp-el", "${1337*1337}"),
    ("ruby/thymeleaf", "#{1337*1337}"),
    ("erb", "<%= 1337*1337 %>"),
    ("smarty/mako", "${{1337*1337}}"),
]
_PARAMS = frozenset({
    "q", "s", "search", "query", "name", "message", "comment", "title", "template", "tpl",
    "page", "id", "input", "text", "value", "lang", "view", "content",
})
_DEFAULT_APPEND = ("q", "search", "name", "message", "id")


def _with_param(url: str, param: str, value: str) -> str:
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q[param] = value
    return urlunparse(parts._replace(query=urlencode(q)))


def _probe(url: str) -> tuple[str, str | None]:
    """(body, error). HTTP errors still return the body (the eval can land on an error page)."""
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-ssti/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - authorized target
            return r.read(2_000_000).decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        try:
            return e.read(1_000_000).decode("utf-8", "replace"), None
        except Exception:  # noqa: BLE001
            return "", None
    except (urllib.error.URLError, OSError) as e:
        return "", str(e)[:160]


@skill(
    name="probe_ssti",
    description=(
        "Test a URL's parameters for server-side template injection (SSTI). Injects template "
        "payloads (Jinja/Twig/FreeMarker/ERB/Smarty) carrying 1337*1337 and flags a parameter when "
        "the evaluated product (1787569) comes back - the server renders input as a template, "
        "often a path to RCE. Tests the URL's params or a small common set; pass param= for one. "
        "Active (sends payloads), authorized only; confirm and escalate before reporting."
    ),
    auto_confirm=False,  # sends injection payloads to the target - gated
    timeout_s=90,
)
async def probe_ssti(url: str, param: str = "") -> dict[str, Any]:
    """Probe a URL's parameters for server-side template injection.

    url: the target URL (ideally one with a parameter, e.g. https://x/page?name=test).
    param: optional single parameter to test; else the URL's params or a small common set.
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
        candidates = [p for p in existing if p.lower() in _PARAMS] or list(_DEFAULT_APPEND)

    def _run() -> tuple[list[dict[str, str]], bool]:
        hits, reachable = [], False
        for p in candidates:
            for engine, payload in _PAYLOADS:
                body, err = _probe(_with_param(url, p, payload))
                if err is None:
                    reachable = True
                if _EXPECT in (body or ""):  # the math was evaluated -> template injection
                    hits.append({"param": p, "engine": engine, "payload": payload})
                    break  # this param confirmed; move to the next
        return hits, reachable

    hits, reachable = await asyncio.to_thread(_run)
    if not reachable:
        return {"error": f"could not reach {url}", "url": url}

    findings = [
        {
            "id": "ssti",
            "severity": "high",
            "matched_at": _with_param(url, h["param"], h["payload"]),
            "name": f"Server-side template injection via '{h['param']}' ({h['engine']})",
            "description": (
                f"The '{h['param']}' parameter evaluated the template payload {h['payload']} to "
                f"{_EXPECT} - the server renders input as a {h['engine']} template (SSTI). This is "
                "often escalatable to RCE; confirm the engine and build the exploit."
            ),
            "curl": f"curl -i '{_with_param(url, h['param'], h['payload'])}'",
        }
        for h in hits
    ]

    return {
        "url": url,
        "tested_params": candidates,
        "hits": hits,
        "findings": findings,
        "vulnerable": bool(findings),
        "note": ("Confirmed evaluation (1337*1337 -> 1787569). Identify the engine and escalate "
                 "to file read / RCE per its sandbox before reporting."),
    }
