"""API surface discovery — finding a target's API spec and enumerating every route from it.

discover_api probes the well-known locations of API specifications (Swagger / OpenAPI / API-docs)
and GraphQL endpoints. When it finds an OpenAPI/Swagger document it PARSES it and extracts every
route + method — the complete API surface, handed to the access-control checks. APIs are where the
highest-paying bugs live (IDOR / BOLA / broken access control), and a public spec or live GraphQL
endpoint maps the whole attack surface for you. Passive: a small fixed set of GETs plus a benign
GraphQL typename query, no payloads. Pure-stdlib (urllib + json), CI-safe, $0.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin, urlparse

from friday.skills.registry import skill

_SPEC_PATHS = (
    "/swagger.json", "/swagger/v1/swagger.json", "/swagger/v2/swagger.json",
    "/v2/swagger.json", "/v3/swagger.json", "/openapi.json", "/v2/api-docs", "/v3/api-docs",
    "/api-docs", "/api/swagger.json", "/api/openapi.json", "/api/v1/openapi.json",
    "/api/v2/swagger.json", "/.well-known/openapi.json", "/api.json", "/swagger/doc.json",
)
_GRAPHQL_PATHS = ("/graphql", "/api/graphql", "/v1/graphql", "/query", "/graphql/console")
_METHODS = ("get", "post", "put", "delete", "patch", "head", "options")


def _get(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-apidisc/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 — authorized target
            return r.status, r.read(4_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(2_000_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return e.code, ""
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return 0, ""


def _post_graphql(url: str) -> tuple[int, str]:
    body = json.dumps({"query": "{__typename}"}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"User-Agent": "sutra-apidisc/1.0", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 — benign typename query
            return r.status, r.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(200_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return e.code, ""
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return 0, ""


def _parse_openapi(text: str) -> list[str]:
    """Extract 'METHOD /route' entries from an OpenAPI/Swagger JSON document."""
    try:
        spec = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(spec, dict):
        return []
    out: list[str] = []
    for route, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for m in methods:
            if m.lower() in _METHODS:
                out.append(f"{m.upper()} {route}")
    return out


@skill(
    name="discover_api",
    description=(
        "Discover a target's API surface. Probes the well-known Swagger/OpenAPI spec locations and "
        "GraphQL endpoints; when it finds an OpenAPI/Swagger doc it parses it and lists every "
        "route and method — the full API surface where IDOR / BOLA / broken-access-control (the "
        "highest-paying classes) live. Confirms live GraphQL endpoints with a benign typename "
        "query (then use graphql_introspect). Passive — a small fixed set of GETs, no payloads. "
        "Feed the routes to find_params / idor_check / check_sqli."
    ),
    auto_confirm=True,  # passive: fixed GETs + a benign GraphQL typename query, no payloads
    timeout_s=60,
)
async def discover_api(url: str) -> dict[str, Any]:
    """Find and enumerate a target's API surface.

    url: the target base URL (e.g. https://api.example.com).
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to scan"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    def _run() -> tuple[list[dict[str, Any]], list[str], list[str], bool]:
        specs: list[dict[str, Any]] = []
        endpoints: list[str] = []
        graphql: list[str] = []
        reachable = False
        for path in _SPEC_PATHS:
            spec_url = urljoin(base + "/", path.lstrip("/"))
            status, body = _get(spec_url)
            if status:
                reachable = True
            low = body.lower()
            if status == 200 and ('"swagger"' in low or '"openapi"' in low) and '"paths"' in low:
                eps = _parse_openapi(body)
                specs.append({"url": spec_url, "type": "openapi/swagger", "endpoints": len(eps)})
                endpoints.extend(eps)
        for path in _GRAPHQL_PATHS:
            gql_url = urljoin(base + "/", path.lstrip("/"))
            status, body = _post_graphql(gql_url)
            if status:
                reachable = True
            if status and ("__typename" in body or '"data"' in body
                           or ("graphql" in body.lower() and "query" in body.lower())):
                graphql.append(gql_url)
        return specs, sorted(dict.fromkeys(endpoints)), sorted(dict.fromkeys(graphql)), reachable

    specs, endpoints, graphql, reachable = await asyncio.to_thread(_run)
    if not reachable:
        return {"error": f"could not reach {url}", "url": url}

    findings: list[dict[str, Any]] = []
    for s in specs:
        findings.append({
            "id": "exposed-api-spec",
            "severity": "info",
            "matched_at": s["url"],
            "name": f"Exposed API specification ({s['endpoints']} routes)",
            "description": (
                f"A public OpenAPI/Swagger spec is served at {s['url']}, exposing {s['endpoints']} "
                "API routes. Map them and test the access-control-sensitive ones (IDOR/BOLA) with "
                "idor_check, and parameters with check_sqli / find_params."
            ),
        })
    for g in graphql:
        findings.append({
            "id": "graphql-endpoint",
            "severity": "info",
            "matched_at": g,
            "name": "Live GraphQL endpoint",
            "description": (f"A live GraphQL endpoint at {g}. Run graphql_introspect to dump the "
                            "schema and look for privileged mutations / IDOR in resolvers."),
        })

    return {
        "url": url,
        "specs": specs,
        "endpoints": endpoints[:500],
        "graphql": graphql,
        "findings": findings,
        "note": (
            f"{len(endpoints)} route(s) from {len(specs)} spec(s); {len(graphql)} GraphQL "
            "endpoint(s). Resolve routes against the host and test access control (idor_check) "
            "and injection (check_sqli / check_ssrf / find_params) — APIs hold the top payouts."
        ),
    }
