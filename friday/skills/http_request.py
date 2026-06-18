"""Generic HTTP skill — call ANY REST API from one tool.

The single biggest reach multiplier: instead of a bespoke skill per service, one
`http_request` lets Sutra hit any endpoint (weather, GitHub, a home-automation box, a
bug-bounty target's API …). Gated (`auto_confirm=False`): it can POST/PUT/DELETE and
reach internal hosts, so it confirms before firing — and in the autonomous loop it is
NOT hands-free, so it always escalates to the operator.
"""

from __future__ import annotations

from typing import Any

import httpx

from friday.skills.registry import skill

_MAX_CHARS = 6000
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
            resp = await client.request(
                method, url,
                headers=headers or None,
                json=json_body if json_body else None,
                content=data.encode() if (data and not json_body) else None,
            )
    except Exception as e:  # noqa: BLE001 — surface the failure, don't raise into the loop
        return {"error": f"request failed: {e}", "url": url}

    out: dict[str, Any] = {
        "status": resp.status_code,
        "url": str(resp.url),
        "content_type": resp.headers.get("content-type", ""),
    }
    if "application/json" in out["content_type"].lower():
        try:
            out["json"] = resp.json()
            return out
        except Exception:  # noqa: BLE001 — malformed JSON → fall back to text
            pass
    out["body"] = resp.text[:max_chars]
    return out
