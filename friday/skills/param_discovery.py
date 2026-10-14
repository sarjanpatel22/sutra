"""Parameter discovery — finding hidden/undocumented query parameters a target actually processes.

find_params probes a URL with a wordlist of common parameter names, each carrying a unique canary
value, and reports the ones whose value is reflected back in the response — proof the app reads it.
Hidden params are where un-fuzzed bugs live: a reflected param is an immediate XSS / SQLi / SSRF /
open-redirect surface to feed the other skills. Batches many names per request to stay fast.
Active (sends probe requests) so it is gated. Pure-stdlib (urllib), CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import http.client
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from friday.skills.registry import skill

# Common parameter names worth probing — redirect/file/debug/callback/etc. are the high-signal ones.
_WORDLIST = (
    "id", "page", "p", "q", "query", "search", "s", "keyword", "cat", "category", "user", "userid",
    "uid", "name", "user_id", "account", "email", "item", "product", "pid", "order", "sort", "dir",
    "url", "uri", "redirect", "redirect_uri", "redirect_url", "return", "returnurl", "next", "dest",
    "destination", "continue", "goto", "callback", "jsonp", "cb", "file", "filename", "path", "dir",
    "folder", "doc", "document", "template", "lang", "language", "locale", "debug", "test", "admin",
    "preview", "view", "mode", "action", "type", "format", "output", "ref", "referrer", "source",
    "site", "host", "domain", "ip", "port", "token", "key", "api_key", "apikey", "access", "role",
    "show", "hide", "filter", "limit", "offset", "start", "end", "from", "to", "date", "year",
    "month", "day", "color", "theme", "style", "width", "height", "image", "img", "data", "json",
    "xml", "html", "text", "msg", "message", "error", "status", "code", "step", "tab", "section",
)
_CHUNK = 25  # params per request — keeps the URL a sane length
# a param name no real app reads; if its canary reflects, the app echoes arbitrary params
_CONTROL = "sutrazznotaparam"


def _probe(url: str) -> tuple[int, str, str | None]:
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-paramfind/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 — authorized target
            return r.status, r.read(500_000).decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        try:
            body = e.read(500_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body, None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        return 0, "", str(e)[:160]


def _with_params(url: str, extra: dict[str, str]) -> str:
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q.update(extra)
    return urlunparse(parts._replace(query=urlencode(q)))


@skill(
    name="find_params",
    description=(
        "Discover hidden/undocumented query parameters a URL actually processes. Probes a wordlist "
        "of common parameter names (id, redirect, file, callback, debug, token, …), each with a "
        "unique canary value, and reports the ones reflected back in the response — proof the app "
        "reads them. Reflected params are an immediate XSS / SQLi / SSRF / open-redirect surface "
        "to feed the other skills. Pass extra= (comma-separated) to add custom names. Active "
        "(sends probe requests) — authorized targets only."
    ),
    auto_confirm=False,  # sends batches of probe requests — gated
    timeout_s=90,
)
async def find_params(url: str, extra: str = "") -> dict[str, Any]:
    """Probe a URL for hidden parameters via canary reflection.

    url: the target URL.
    extra: optional comma-separated extra parameter names to include in the wordlist.
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to test"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    words = list(dict.fromkeys([*_WORDLIST, *[w.strip() for w in extra.split(",") if w.strip()]]))

    def _run() -> tuple[list[dict[str, Any]], bool, bool]:
        discovered: list[dict[str, Any]] = []
        reachable = False
        echoes_all = False
        for i in range(0, len(words), _CHUNK):
            chunk = words[i : i + _CHUNK]
            # each candidate gets a unique canary so we can tell which one reflected
            canaries = {w: f"sfp{i + j}zz" for j, w in enumerate(chunk)}
            canaries[_CONTROL] = f"sfpctrl{i}zz"  # control: a param the app can't legitimately read
            status, body, err = _probe(_with_params(url, canaries))
            if err is not None:
                continue
            reachable = True
            if canaries[_CONTROL] in (body or ""):
                echoes_all = True  # the app reflects arbitrary params → reflection proves nothing
                break
            for w, token in canaries.items():
                if w != _CONTROL and token in (body or ""):
                    discovered.append({"param": w, "canary": token, "status": status})
        return discovered, reachable, echoes_all

    discovered, reachable, echoes_all = await asyncio.to_thread(_run)
    if not reachable:
        return {"error": f"could not reach {url}", "url": url}
    if echoes_all:
        return {
            "url": url,
            "tested": len(words),
            "discovered": [],
            "details": [],
            "findings": [],
            "note": (
                "Target reflects arbitrary parameters (it echoes the query string), so reflection "
                "can't distinguish real params here — discovery skipped to avoid false positives. "
                "Use response-diff/wordlist tooling (e.g. arjun, x8) against this endpoint instead."
            ),
        }

    names = [d["param"] for d in discovered]
    findings = (
        [
            {
                "id": "reflected-parameter",
                "severity": "info",
                "matched_at": url,
                "name": f"{len(names)} reflected parameter(s) discovered",
                "description": (
                    "These parameters are read by the app and reflected in the response: "
                    f"{', '.join(names)}. Each is an injection surface — test with check_sqli, "
                    "test_reflected_xss, check_open_redirect and check_crlf."
                ),
            }
        ]
        if names
        else []
    )

    return {
        "url": url,
        "tested": len(words),
        "discovered": names,
        "details": discovered,
        "findings": findings,
        "note": (
            "Reflection proves the param is processed; feed the discovered names to the injection "
            "skills (check_sqli / test_reflected_xss / check_open_redirect / check_crlf) to test "
            "each for an actual vulnerability."
        ),
    }
