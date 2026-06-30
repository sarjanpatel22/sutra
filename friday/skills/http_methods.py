"""HTTP method audit — which verbs does an endpoint allow, and are any of them dangerous?

Surfaces three checklist findings without ever sending a state-changing request:
  - TRACE enabled → Cross-Site Tracing (XST), confirmed when the server echoes our marker back.
  - PUT / DELETE / PATCH advertised in the OPTIONS `Allow` header → potential file upload/deletion
    (reported as a lead to verify by hand — we never actually send them).
  - The full allowed-method set, for verb-tampering follow-up (a path that's 403 to GET may answer
    to another verb).

SAFE: only OPTIONS (read-only) and TRACE (echoes, changes nothing) are sent — never PUT/DELETE/POST.
TRACE/XST is confirmed by signature (our marker reflected), not guessed. Auto-confirmed (no
state-changing traffic). Pure-stdlib (urllib), CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from typing import Any

from friday.skills.registry import skill

_DANGEROUS = ("PUT", "DELETE", "PATCH", "CONNECT")  # advertised → report, never invoke
_MARKER = "X-Sutra-Xst: sutra-trace-probe"


class _MethodRequest(urllib.request.Request):
    """A Request that lets us set an arbitrary method (OPTIONS / TRACE)."""

    def __init__(self, url: str, method: str, **kw: Any) -> None:
        super().__init__(url, **kw)
        self._method = method

    def get_method(self) -> str:
        return self._method


def _send(url: str, method: str, headers: dict[str, str] | None = None
          ) -> tuple[dict[str, str], str, int] | None:
    """Send one request with the given method; return (headers, body, status) or None on error."""
    hdrs = {"User-Agent": "sutra-methods/1.0", **(headers or {})}
    req = _MethodRequest(url, method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=12) as r:  # noqa: S310 — authorized target
            return ({k.lower(): v for k, v in r.headers.items()},
                    r.read(100_000).decode("utf-8", "replace"), r.status)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(100_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return {k.lower(): str(v) for k, v in e.headers.items()}, body, e.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _probe(url: str) -> dict[str, Any]:
    """Run the OPTIONS + TRACE audit. Sync (run in a thread)."""
    out: dict[str, Any] = {"allowed": [], "dangerous": [], "trace_xst": False}
    opts = _send(url, "OPTIONS")
    if opts is not None:
        allow = opts[0].get("allow", "")
        methods = [m.strip().upper() for m in allow.split(",") if m.strip()]
        out["allowed"] = methods
        out["dangerous"] = [m for m in methods if m in _DANGEROUS]
    # TRACE → XST: confirmed only if our injected marker is echoed back in the body
    trace = _send(url, "TRACE", headers={"X-Sutra-Xst": "sutra-trace-probe"})
    if trace is not None and trace[2] == 200 and "sutra-trace-probe" in trace[1]:
        out["trace_xst"] = True
    return out


@skill(
    name="check_http_methods",
    description=(
        "Audit which HTTP methods an endpoint allows and flag the dangerous ones. Confirms TRACE / "
        "Cross-Site Tracing (XST) by echoed marker, reports PUT/DELETE/PATCH advertised in the "
        "OPTIONS Allow header (a file upload/deletion lead), and returns the full allowed-method "
        "set for verb tampering. SAFE — only sends OPTIONS and TRACE, never a state-changing "
        "verb. Authorized targets only."
    ),
    auto_confirm=True,  # only OPTIONS (read-only) + TRACE (echoes); no state-changing requests
    timeout_s=30,
)
async def check_http_methods(url: str) -> dict[str, Any]:
    """Audit an endpoint's allowed HTTP methods (OPTIONS + TRACE only).

    url: the target URL (a bare host gets http:// prepended).
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to audit"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    res = await asyncio.to_thread(_probe, url)
    findings: list[dict[str, Any]] = []
    if res["trace_xst"]:
        findings.append({
            "id": "http-trace-xst", "name": "HTTP TRACE enabled (Cross-Site Tracing / XST)",
            "severity": "low", "matched_at": url,
            "description": "The server honoured TRACE and echoed our injected header back — XST. "
                           "Can expose headers/cookies in some legacy setups. Disable TRACE.",
            "curl": f"curl -i -X TRACE '{url}' -H '{_MARKER}'",
        })
    for m in res["dangerous"]:
        findings.append({
            "id": f"http-method-{m.lower()}", "name": f"Dangerous method {m} advertised",
            "severity": "medium" if m in ("PUT", "DELETE") else "low", "matched_at": url,
            "description": f"OPTIONS advertises {m} in its Allow header. If actually honoured this "
                           f"could allow file upload/deletion — VERIFY by hand (we did NOT send "
                           f"{m}). Confirm it changes state before reporting.",
            "curl": f"curl -i -X OPTIONS '{url}'",
        })
    return {
        "url": url,
        "allowed_methods": res["allowed"],
        "dangerous_methods": res["dangerous"],
        "trace_xst": res["trace_xst"],
        "findings": findings,
        "note": (
            f"Allowed: {', '.join(res['allowed']) or 'none advertised'}. "
            f"{len(findings)} item(s) to check. Dangerous methods are LEADS — they're advertised, "
            "not proven to work; verify each actually changes state, in scope, before reporting."
        ),
    }
