"""Generic HTTP skill — call ANY REST API from one tool.

The single biggest reach multiplier: instead of a bespoke skill per service, one
`http_request` lets Sutra hit any endpoint (weather, GitHub, a home-automation box, a
bug-bounty target's API …). Gated (`auto_confirm=False`): it can POST/PUT/DELETE and
reach internal hosts, so it confirms before firing — and in the autonomous loop it is
NOT hands-free, so it always escalates to the operator.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from friday.skills.registry import skill

_MAX_CHARS = 6000
# Hard ceiling on bytes pulled from the response: a hostile/misbehaving endpoint can return an
# unbounded (multi-GB) body, and reading it whole (resp.text/.json) would OOM the process before
# the max_chars slice ever runs. We stream and stop at this cap. 10 MB is far above any real API
# payload a tool call needs; a larger body is truncated honestly (truncated=True).
_MAX_BYTES = 10_000_000
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


@skill(
    name="http_request",
    description=(
        "Make an HTTP request to any URL and return the status, content-type, and body — the "
        "general way to call a REST API or fetch a resource. method = GET/POST/PUT/PATCH/DELETE; "
        "pass headers and a json body as needed. Gated: it can send data and reach internal "
        "hosts, so it asks first."
    ),
    auto_confirm=False,  # external I/O, can mutate (POST/DELETE) and reach internal hosts
    timeout_s=35,
)
async def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    data: str = "",
    timeout: float = 20.0,
    max_chars: int = _MAX_CHARS,
) -> dict[str, Any]:
    """Call an HTTP endpoint. Returns {status, content_type, json|body, url}.

    url: full http(s) URL. method: HTTP verb. headers: optional dict. json_body: a dict
    sent as a JSON payload. data: a raw string body (used when json_body is empty).
    """
    method = (method or "GET").upper()
    if method not in _METHODS:
        return {"error": f"unsupported method {method!r}; use one of {sorted(_METHODS)}"}
    if not url.lower().startswith(("http://", "https://")):
        return {"error": "url must start with http:// or https://"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            async with client.stream(
                method, url,
                headers=headers or None,
                json=json_body if json_body else None,
                content=data.encode() if (data and not json_body) else None,
            ) as resp:
                body = bytearray()
                capped = False
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) >= _MAX_BYTES:  # stop before an unbounded body can OOM us
                        capped = True
                        break
                status = resp.status_code
                content_type = resp.headers.get("content-type", "")
                final_url = str(resp.url)
                encoding = resp.encoding or "utf-8"
    except Exception as e:  # noqa: BLE001 — surface the failure, don't raise into the loop
        return {"error": f"request failed: {e}", "url": url}

    text = bytes(body).decode(encoding, "replace")
    out: dict[str, Any] = {"status": status, "url": final_url, "content_type": content_type}
    if capped:
        out["truncated"] = True  # body exceeded the size cap; what follows is the first _MAX_BYTES
    # Only parse JSON from a complete body — a truncated one isn't valid JSON anyway.
    if not capped and "application/json" in content_type.lower():
        try:
            out["json"] = json.loads(text)
            return out
        except Exception:  # noqa: BLE001 — malformed JSON → fall back to text
            pass
    out["body"] = text[:max_chars]
    return out
