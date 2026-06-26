"""Access-control / IDOR / BOLA testing — the #1 PAYABLE, scanner-blind bug class.

A scanner can't find broken access control: it doesn't have two accounts. This skill does. Give it a
URL that returns account A's private data plus A's session, and (optionally) account B's session; it
replays the SAME request as B and as an unauthenticated client and flags where B or anon gets A's
data back (a 200 whose body matches A's) instead of being denied (401/403). To cut false positives
it suppresses the B finding when the resource is reachable anonymously too (i.e. it's just public).

DETERMINISTIC — it only compares the REAL responses, so it never invents a finding; you confirm the
data is genuinely another user's private data and submit. Active (it sends requests with the auth
you provide, and POST/PUT can change state) and authorized-targets-only, so it is gated.
"""

from __future__ import annotations

import asyncio
import difflib
import urllib.error
import urllib.request
from typing import Any

from friday.skills.registry import skill

_SIMILAR = 0.95  # body-similarity at/above which a response "is" account A's resource. High on
# purpose: a true IDOR returns A's near-exact response, whereas a user's OWN
# same-template page differs in the real data — so a high bar cuts false hits.
_CMP_CAP = 20_000  # cap bodies before the O(n^2) similarity compare — keeps it fast and bounded


def _parse_auth(spec: str) -> dict[str, str]:
    """Turn an auth spec into a request header. 'Name: value' becomes that header; a bare
    string with no colon is a Cookie value ('session=abc' -> Cookie: session=abc). Empty -> {}."""
    spec = (spec or "").strip()
    if not spec:
        return {}
    if ":" in spec:
        name, _, value = spec.partition(":")
        return {name.strip(): value.strip()}
    return {"Cookie": spec}


def _fetch(
    url: str, method: str, headers: dict[str, str], data: bytes | None
) -> tuple[int, str, str | None]:
    """(status, body, error). A 4xx/5xx is a NORMAL result here (we want to see 401/403), not an
    error; only a transport failure (DNS/connection/timeout) returns an error string."""
    req = urllib.request.Request(
        url, method=method, data=data, headers={"User-Agent": "sutra-idor/1.0", **headers}
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:  # noqa: S310 — authorized target
            return r.status, r.read(2_000_000).decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        try:
            body = e.read(500_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body, None
    except (urllib.error.URLError, OSError) as e:
        return 0, "", str(e)[:160]


def _similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a[:_CMP_CAP], b[:_CMP_CAP]).ratio()


def _curl(url: str, method: str, headers: dict[str, str], body: str) -> str:
    parts = ["curl", "-i"]
    if method != "GET":
        parts += ["-X", method]
    for k, v in headers.items():
        parts += ["-H", f"'{k}: {v}'"]
    if body:
        parts += ["--data", f"'{body}'"]
    parts.append(f"'{url}'")
    return " ".join(parts)


@skill(
    name="idor_check",
    description=(
        "Test a URL for broken access control / IDOR / BOLA — the #1 payable bug class a scanner "
        "CANNOT find (it needs two accounts). Give the URL that returns account A's private data "
        "and A's session/auth, optionally account B's session; it replays the request as B and as "
        "an unauthenticated client and flags where they get A's data back (a 200 matching A's "
        "body) instead of being denied (401/403). Suppresses the B finding if the resource is "
        "public (anon sees it too). Compares the REAL responses — no guessing. auth values look "
        "like 'Cookie: session=...' or 'Authorization: Bearer ...' (a bare string is a cookie). "
        "Active, authorized targets only — you verify the data is private and submit."
    ),
    auto_confirm=False,  # sends requests with real auth (POST/PUT can change state) — gated
    timeout_s=60,
)
async def idor_check(
    url: str,
    auth_a: str,
    auth_b: str = "",
    method: str = "GET",
    body: str = "",
    test_anon: bool = True,
) -> dict[str, Any]:
    """Replay one request across accounts to detect broken access control.

    url: the endpoint that returns account A's private data (e.g. https://x/api/users/1/profile).
    auth_a: account A's auth — 'Cookie: k=v' or 'Authorization: Bearer ...' (bare string = cookie).
    auth_b: account B's auth (optional) — cross-account test; omit to test only anonymous access.
    method: HTTP method (GET default).
    body: request body for POST/PUT.
    test_anon: also replay with NO auth, to catch unauthenticated access (default True).
    """
    url = (url or "").strip()
    if not url:
        return {"error": "no url to test"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    if not (auth_a or "").strip():
        return {
            "error": "auth_a is required — it establishes what account A's private data looks like"
        }
    auth_b = (auth_b or "").strip()
    if not auth_b and not test_anon:
        return {"error": "nothing to compare — provide auth_b and/or leave test_anon on"}
    method = (method or "GET").strip().upper()
    data = body.encode() if body else None

    def _run_all() -> tuple[tuple, tuple | None, tuple | None]:
        a = _fetch(url, method, _parse_auth(auth_a), data)
        b = _fetch(url, method, _parse_auth(auth_b), data) if auth_b else None
        anon = _fetch(url, method, {}, data) if test_anon else None
        return a, b, anon

    (sa, ba, ea), b_res, anon_res = await asyncio.to_thread(_run_all)
    if ea:
        return {"error": f"could not reach the target as account A: {ea}", "url": url}

    tests: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    warnings: list[str] = []
    if sa != 200:
        warnings.append(
            f"account A got HTTP {sa}, not 200 — check auth_a; the baseline may be wrong"
        )

    # Assess anon first so we know whether the resource is simply PUBLIC (anon sees it too).
    anon_is_a = False
    if anon_res is not None:
        st, bd, err = anon_res
        if err:
            tests.append({"account": "anon", "error": err})
        else:
            sim = _similarity(ba, bd)
            anon_is_a = st == 200 and sim >= _SIMILAR
            entry = {
                "account": "anon",
                "status": st,
                "length": len(bd),
                "similarity": round(sim, 3),
            }
            if anon_is_a:
                entry["verdict"] = (
                    "VULNERABLE — unauthenticated client received account A's data "
                    f"({int(sim * 100)}% match); verify this endpoint should require auth"
                )
                entry["vulnerable"] = True
                findings.append(
                    {
                        "id": "idor-unauthenticated-access",
                        "name": "Broken access control — unauthenticated access to a user resource",
                        "severity": "critical",
                        "matched_at": url,
                        "description": (
                            f"An unauthenticated request to {url} returned content matching "
                            f"account A's private resource (HTTP {st}, {int(sim * 100)}% body "
                            "match). Verify the data is genuinely private and not an "
                            "intentionally public page before reporting."
                        ),
                        "curl": _curl(url, method, {}, body),
                    }
                )
            else:
                entry["verdict"] = (
                    f"properly denied ({st})"
                    if st in (401, 403)
                    else f"status {st} — not account A's data, OK"
                )
                entry["vulnerable"] = False
            tests.append(entry)

    if b_res is not None:
        st, bd, err = b_res
        if err:
            tests.append({"account": "B", "error": err})
        else:
            sim = _similarity(ba, bd)
            entry = {"account": "B", "status": st, "length": len(bd), "similarity": round(sim, 3)}
            if st == 200 and sim >= _SIMILAR and not anon_is_a:
                entry["verdict"] = (
                    "VULNERABLE — account B received account A's data "
                    f"({int(sim * 100)}% match); broken object/function-level access control"
                )
                entry["vulnerable"] = True
                findings.append(
                    {
                        "id": "idor-cross-account-access",
                        "name": "Broken access control (IDOR/BOLA) — another user can read it",
                        "severity": "high",
                        "matched_at": url,
                        "description": (
                            f"Account B retrieved content matching account A's private resource "
                            f"at {url} (HTTP {st}, {int(sim * 100)}% body match) — broken "
                            "object/function-level access control. Confirm the data is "
                            "genuinely account A's before reporting."
                        ),
                        "curl": _curl(url, method, _parse_auth(auth_b), body),
                    }
                )
            elif st == 200 and sim >= _SIMILAR and anon_is_a:
                entry["verdict"] = (
                    "200 matching A, but the resource is reachable anonymously too "
                    "(public) — not access control"
                )
                entry["vulnerable"] = False
            elif st in (401, 403):
                entry["verdict"] = f"properly denied ({st})"
                entry["vulnerable"] = False
            else:
                entry["verdict"] = (
                    f"200 but different content ({int(sim * 100)}% match) — likely B's "
                    "own data; verify"
                    if st == 200
                    else f"status {st} — verify manually"
                )
                entry["vulnerable"] = False
            tests.append(entry)

    return {
        "url": url,
        "method": method,
        "baseline": {"account": "A", "status": sa, "length": len(ba)},
        "tests": tests,
        "findings": findings,
        "vulnerable": bool(findings),
        "warnings": warnings,
        "note": (
            "Confirm the matched content is genuinely another user's PRIVATE data before "
            "reporting — pages that merely share a template, or a public page anon also sees, "
            "can match without being IDOR."
        ),
    }
