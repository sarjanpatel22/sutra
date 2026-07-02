"""NoSQL injection detection — confirmed error-based + operator-differential.

check_nosqli targets MongoDB/NoSQL back-ends, which build queries from user input differently than
SQL: a parameter sent as `user[$ne]=x` is parsed (Express/qs, PHP) into `{user: {$ne: "x"}}` and, if
the app trusts it, becomes a query OPERATOR — the root of NoSQL auth bypass (`user[$ne]=&pass[$ne]=`
logs in as the first user) and data exposure. It confirms two ways:
(1) ERROR-BASED — a broken-operator/JSON payload provokes a Mongo/Mongoose error string absent from
the clean baseline (MongoError, CastError, "unknown operator", E11000…) — a confirmed find;
(2) OPERATOR-DIFFERENTIAL — `param[$ne]=<nonexistent>` (always-true → a data page) vs
`param[$eq]=<nonexistent>` (always-false → empty) return meaningfully different responses,
reproduced to kill variance — a strong candidate (a non-injectable app treats `param[$ne]` as a
literal key, so both look the same). Active (crafted requests) → gated. Pure-stdlib, CI-safe. Verify
and scope before reporting; never dump production data or actually bypass a real login.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from friday.skills.registry import skill

# (engine, error fingerprint). A match present in a payload response but NOT the clean baseline =
# confirmed NoSQL injection. Kept engine-specific (no generic "error"/"exception") for precision.
_ERROR_SIGS: list[tuple[str, re.Pattern[str]]] = [
    ("MongoDB", re.compile(
        r"MongoError|MongoServerError|MongoServerSelectionError|com\.mongodb|MongooseError|"
        r"BSONError|E11000 duplicate key|\$where is not allowed|unknown (?:top level )?operator|"
        r"Cast to \w+ failed for value|can't \$divide|BadValue", re.I)),
    ("CouchDB", re.compile(r"query_parse_error|\"error\"\s*:\s*\"(?:bad_request|badmatch)", re.I)),
]
# Broken-operator / broken-JS payloads that tend to fault a Mongo query builder ($where JS, JSON).
_ERR_PAYLOADS = ("'", '"', "'\"", "\\", "';return(true);var x='", "'||'1'=='1")
_OPERATORS_ID = frozenset(
    {"id", "page", "cat", "category", "user", "userid", "uid", "pid", "item", "product",
     "search", "q", "query", "name", "username", "email", "login", "account", "order", "sort",
     "view", "ref", "num", "role"}
)
_DEFAULT_APPEND = ("id", "user", "username", "q", "search", "name")


def _set_param(url: str, param: str, value: str) -> str:
    """Replace a plain query param with a literal value (used for baseline + error payloads)."""
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q[param] = value
    return urlunparse(parts._replace(query=urlencode(q)))


def _set_op(url: str, param: str, op: str, value: str) -> str:
    """Inject an operator form: drop `param` (and any `param[...]`), add `param[$op]=value` so the
    backend that parses nested keys receives {param: {$op: value}}."""
    parts = urlparse(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k != param and not k.startswith(param + "[")]
    kept.append((f"{param}[${op}]", value))
    return urlunparse(parts._replace(query=urlencode(kept)))


def _orig_value(url: str, param: str) -> str:
    for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True):
        if k == param:
            return v or "1"
    return "1"


def _probe(url: str) -> tuple[int, str, str | None]:
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-nosqli/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 — authorized target
            return r.status, r.read(500_000).decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        try:
            body = e.read(500_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body, None
    except (urllib.error.URLError, OSError) as e:
        return 0, "", str(e)[:160]


def _engine_error(body: str, baseline: str) -> tuple[str, str] | None:
    """Return (engine, matched) if an error fingerprint is in body but not the clean baseline."""
    for engine, pat in _ERROR_SIGS:
        m = pat.search(body or "")
        if m and not pat.search(baseline or ""):
            return engine, m.group(0)[:120]
    return None


@skill(
    name="check_nosqli",
    description=(
        "Test a URL for NoSQL (MongoDB) injection — operator injection via `param[$ne]`/`[$gt]`/"
        "`[$regex]` that enables auth bypass and data exposure, a class check_sqli can't see. "
        "Confirms two ways: error-based (a broken-operator payload provokes a Mongo/Mongoose error "
        "absent from the baseline) and operator-differential (`[$ne]=<nonexistent>` returns a data "
        "page, `[$eq]=<nonexistent>` returns empty, reproduced). Auto-targets id/user/search "
        "params or a common set; pass param= for one. Active (crafted requests) — authorized "
        "targets only; verify and scope before reporting, never bypass a real login or dump data."
    ),
    auto_confirm=False,  # sends crafted NoSQL operator payloads — gated
    timeout_s=90,
)
async def check_nosqli(url: str, param: str = "") -> dict[str, Any]:
    """Probe a URL's parameters for error-based and operator-differential NoSQL injection.

    url: the target URL (ideally with a data param, e.g. https://x/item?id=1).
    param: optional single parameter to test; otherwise query params are used, or a common set.
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
        candidates = [p for p in existing if p.lower() in _OPERATORS_ID] or list(_DEFAULT_APPEND)
    nonexist = "sutra" + secrets.token_hex(5)  # a value overwhelmingly unlikely to exist

    def _run() -> tuple[list[dict[str, Any]], bool]:
        findings: list[dict[str, Any]] = []
        _, baseline, berr = _probe(url)
        reachable = berr is None
        base_len = len(baseline or "")
        tol = base_len * 0.05 + 12  # "≈ baseline" tolerance
        gap = base_len * 0.25 + 20  # "meaningfully different" threshold

        def close(a: int, b: int) -> bool:
            return abs(a - b) <= tol

        def far(a: int, b: int) -> bool:
            return abs(a - b) >= gap

        # page-stability gate: a non-deterministic page makes the differential meaningless.
        diff_ok = base_len > 0
        if diff_ok:
            _, base2, be2 = _probe(url)
            if be2 is not None or not close(len(base2), base_len):
                diff_ok = False
        for p in candidates:
            orig = _orig_value(url, p)
            # 1) error-based (confirmed)
            hit = False
            for payload in _ERR_PAYLOADS:
                status, body, err = _probe(_set_param(url, p, orig + payload))
                if err is None:
                    reachable = True
                got = _engine_error(body, baseline)
                if got:
                    engine, matched = got
                    findings.append({
                        "id": "nosqli-error", "param": p, "severity": "high", "engine": engine,
                        "payload": orig + payload, "status": status, "evidence": matched,
                        "confidence": "confirmed",
                    })
                    hit = True
                    break
            if hit or not diff_ok:
                continue
            # 2) operator-differential (candidate) — reproduced to rule out a fluke
            _, t_body, te = _probe(_set_op(url, p, "ne", nonexist))   # always-true → data page
            _, f_body, fe = _probe(_set_op(url, p, "eq", nonexist))   # always-false → empty
            if te or fe:
                continue
            tl, fl = len(t_body), len(f_body)
            if not (close(tl, base_len) and far(tl, fl)):
                continue
            _, t2, te2 = _probe(_set_op(url, p, "ne", nonexist))
            _, f2, fe2 = _probe(_set_op(url, p, "eq", nonexist))
            if te2 or fe2 or not (close(len(t2), base_len) and far(len(t2), len(f2))):
                continue
            findings.append({
                "id": "nosqli-operator", "param": p, "severity": "high",
                "payload": f"{p}[$ne]={nonexist} / {p}[$eq]={nonexist}", "confidence": "candidate",
                "evidence": f"$ne≈base ({tl}≈{base_len}B), $eq≠ ({fl}B); reproduced",
            })
        return findings, reachable

    findings, reachable = await asyncio.to_thread(_run)
    if not reachable:
        return {"error": f"could not reach {url}", "url": url}

    out_findings: list[dict[str, Any]] = []
    for f in findings:
        kind = f.get("engine", "operator")
        out_findings.append({
            "id": f["id"],
            "severity": f["severity"],
            "matched_at": url,
            "name": f"NoSQLi ({kind}/{f['confidence']}) via '{f['param']}'",
            "description": (
                f"The '{f['param']}' parameter reaches a NoSQL query ({f['confidence']}): payload "
                f"'{f['payload']}' → {f['evidence']}. NoSQL operator injection enables auth bypass "
                "(user[$ne]=&pass[$ne]=) and data exposure — verify manually and scope before "
                "reporting; never bypass a real login or dump production data."
            ),
        })

    return {
        "url": url,
        "tested_params": candidates,
        "findings": out_findings,
        "vulnerable": bool(out_findings),
        "note": (
            "Error-based hits are confirmed; operator-differential hits are strong candidates — "
            "verify with a manual [$ne]/[$eq] differential (or a JSON-body {\"$ne\":\"\"} on login "
            "endpoints) before reporting, and never exfiltrate production data."
        ),
    }
