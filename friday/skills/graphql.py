"""GraphQL introspection — map the API and flag exposed introspection + sensitive mutations.

graphql_introspect POSTs the standard introspection query: if the endpoint answers with a schema,
introspection is ENABLED (information disclosure — the whole API is mapped for an attacker) and the
queries/mutations are dumped, with the privileged-looking mutations (delete*/update*/setRole/
resetPassword/…) called out as missing-authorization (BFLA) test targets. The coach already points
hunters at GraphQL; this gives it a real tool. One read-only POST, pure-stdlib, CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from typing import Any

from friday.skills.registry import skill

# Compact introspection query — type names + the Query/Mutation field lists are all we need to map
# the surface (we skip the full type graph to keep the request and parse small).
_QUERY = (
    "query IntrospectionQuery{__schema{queryType{name}mutationType{name}"
    "types{name kind fields{name}}}}"
)

# Mutation names that imply a state change worth an access-control test once enumerated.
_SENSITIVE = re.compile(
    r"(?i)(delete|remove|drop|update|edit|set|create|add|register|reset|password|admin|role|grant|"
    r"revoke|promote|disable|enable|upload|import|exec|approve|refund|transfer|purchase)"
)


def _post(url: str) -> tuple[int, dict[str, Any] | None, str | None]:
    """(status, parsed-json, error). Transport failure → error; non-JSON → parsed None."""
    body = json.dumps({"query": _QUERY}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "sutra-graphql/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:  # noqa: S310 — authorized target
            raw = r.read(3_000_000).decode("utf-8", "replace")
            status = r.status
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
    name="graphql_introspect",
    description=(
        "Probe a GraphQL endpoint for introspection. POSTs the introspection query; if the schema "
        "comes back, introspection is ENABLED (info disclosure — the full API is mapped) and "
        "this returns the queries and mutations, flagging the privileged-looking mutations "
        "(delete/update/setRole/resetPassword/…) as missing-authorization (BFLA) test targets. A "
        "single read-only POST. Verify the sensitive operations lack authz before reporting."
    ),
    auto_confirm=True,  # one read-only introspection POST (no mutation executed)
    timeout_s=30,
)
async def graphql_introspect(url: str) -> dict[str, Any]:
    """Introspect a GraphQL endpoint and map its surface.

    url: the GraphQL endpoint (e.g. https://host/graphql; a bare host defaults to http://).
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to introspect"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    status, data, err = await asyncio.to_thread(_post, url)
    if err:
        return {"error": f"could not reach {url}: {err}", "url": url}
    schema = (data or {}).get("data", {}).get("__schema") if isinstance(data, dict) else None
    if not schema:
        return {
            "url": url,
            "introspection_enabled": False,
            "findings": [],
            "vulnerable": False,
            "note": "Introspection appears disabled (no schema returned) — not a finding here. "
            "Try common paths (/graphql, /api/graphql, /v1/graphql) if you expected one.",
        }

    by_name = {t.get("name"): t for t in schema.get("types") or [] if isinstance(t, dict)}

    def _fields(type_ref: dict[str, Any] | None) -> list[str]:
        t = by_name.get((type_ref or {}).get("name")) if type_ref else None
        return [f.get("name") for f in (t or {}).get("fields") or [] if f.get("name")]

    queries = _fields(schema.get("queryType"))
    mutations = _fields(schema.get("mutationType"))
    sensitive = [m for m in mutations if _SENSITIVE.search(m)]

    findings: list[dict[str, Any]] = [
        {
            "id": "graphql-introspection-enabled",
            "severity": "medium",
            "matched_at": url,
            "name": "GraphQL introspection is enabled",
            "description": (
                f"{url} answers the introspection query — the full schema is exposed "
                f"({len(queries)} queries, {len(mutations)} mutations across "
                f"{len(by_name)} types). Information disclosure that maps the whole attack "
                "surface; disable introspection in production."
            ),
        }
    ]
    if sensitive:
        findings.append(
            {
                "id": "graphql-sensitive-mutations",
                "severity": "low",
                "matched_at": url,
                "name": f"Privileged mutations exposed: {', '.join(sensitive[:12])}",
                "description": (
                    f"State-changing mutations {sensitive} are reachable. Call each as a "
                    "low-privileged / unauthenticated user and check for missing "
                    "authorization (function-level access control / BFLA) — the payable bug."
                ),
            }
        )

    return {
        "url": url,
        "introspection_enabled": True,
        "queries": queries,
        "mutations": mutations,
        "sensitive_mutations": sensitive,
        "type_count": len(by_name),
        "findings": findings,
        "vulnerable": True,
        "note": (
            "Introspection alone is usually low/medium; the money is testing the exposed "
            "mutations/queries for missing authorization (BFLA/IDOR) — verify before reporting."
        ),
    }
