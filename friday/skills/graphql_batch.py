"""GraphQL batching abuse — detecting alias / array batching that bypasses rate limits.

graphql_batch checks whether a GraphQL endpoint executes many operations in a single request, two
ways: ALIAS batching (one query with N aliases of the same field) and ARRAY batching (a JSON array
of query objects). Either lets an attacker fire hundreds of operations per request — bypassing
per-request rate limits to brute-force logins/OTP/coupons or amplify an IDOR across many object IDs
at once (the primitive behind several high-payout GraphQL chains). Confirmed when the endpoint
actually returns the batched results. Active (crafted POSTs) so it is gated. Pure-stdlib, $0.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any

from friday.skills.registry import skill

_ALIASES = 12  # how many aliased ops to pack into the single test query
_ARRAY = 5  # how many queries in the array-batch test


def _post(url: str, body: bytes) -> tuple[int, Any, str | None]:
    """(status, parsed-json, error) for a raw GraphQL POST body."""
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "sutra-gqlbatch/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:  # noqa: S310 — authorized target
            raw, status = r.read(2_000_000).decode("utf-8", "replace"), r.status
    except urllib.error.HTTPError as e:
        try:
            raw, status = e.read(1_000_000).decode("utf-8", "replace"), e.code
        except Exception:  # noqa: BLE001
            return e.code, None, None
    except (urllib.error.URLError, OSError) as e:
        return 0, None, str(e)[:160]
    try:
        return status, json.loads(raw), None
    except (ValueError, json.JSONDecodeError):
        return status, None, None


@skill(
    name="graphql_batch",
    description=(
        "Test a GraphQL endpoint for batching abuse — executing many operations in one request to "
        "bypass rate limits. Checks alias batching (one query with N aliased fields) and array "
        "batching (a JSON array of queries); if the endpoint returns all the batched results, an "
        "attacker can brute-force logins/OTP/coupons or amplify IDOR across many IDs per request. "
        "Confirmed-only (the batched results must actually come back). Active (crafted POSTs) — "
        "authorized targets only; then weaponise against a real mutation (e.g. login) to prove it."
    ),
    auto_confirm=False,  # sends crafted batched POSTs — gated
    timeout_s=40,
)
async def graphql_batch(url: str) -> dict[str, Any]:
    """Probe a GraphQL endpoint for alias and array request batching.

    url: the GraphQL endpoint (e.g. https://host/graphql).
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to test"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    def _run() -> tuple[bool, int, bool, int, bool]:
        reachable = False
        # 1) alias batching: { a0:__typename a1:__typename ... }
        alias_q = "{" + " ".join(f"a{i}:__typename" for i in range(_ALIASES)) + "}"
        st, data, err = _post(url, json.dumps({"query": alias_q}).encode())
        if err is None:
            reachable = True
        alias_n = 0
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            alias_n = sum(1 for k in data["data"] if k.startswith("a"))
        alias_ok = alias_n >= max(2, _ALIASES // 2)
        # 2) array batching: [{query},{query},...]
        st2, data2, err2 = _post(url, json.dumps([{"query": "{__typename}"}] * _ARRAY).encode())
        if err2 is None:
            reachable = True
        array_n = len(data2) if isinstance(data2, list) else 0
        array_ok = array_n >= 2 and all(isinstance(x, dict) for x in data2)
        return alias_ok, alias_n, array_ok, array_n, reachable

    alias_ok, alias_n, array_ok, array_n, reachable = await asyncio.to_thread(_run)
    if not reachable:
        return {"error": f"could not reach {url}", "url": url}

    findings: list[dict[str, Any]] = []
    if alias_ok:
        findings.append({
            "id": "graphql-alias-batching",
            "severity": "medium",
            "matched_at": url,
            "name": "GraphQL alias batching allowed",
            "description": (
                f"The endpoint executed {alias_n} aliased operations in a single query. An "
                "attacker can pack hundreds of aliased mutations into one request to bypass "
                "per-request rate limits — brute-force login/OTP/coupon endpoints or amplify IDOR "
                "across many IDs. Weaponise against a real mutation (e.g. login) to prove it."
            ),
        })
    if array_ok:
        findings.append({
            "id": "graphql-array-batching",
            "severity": "medium",
            "matched_at": url,
            "name": "GraphQL array request batching allowed",
            "description": (
                f"The endpoint returned {array_n} results for a batched array of queries. Same "
                "impact as alias batching — many operations per request, defeating rate limits. "
                "Confirm against an auth/OTP mutation before reporting."
            ),
        })

    return {
        "url": url,
        "alias_batching": alias_ok,
        "array_batching": array_ok,
        "findings": findings,
        "vulnerable": bool(findings),
        "note": (
            "Batching itself is the primitive; the payable bug is proving it bypasses a real "
            "rate limit (brute-force) or amplifies IDOR. Pair with graphql_introspect to find the "
            "mutation to weaponise."
        ),
    }
