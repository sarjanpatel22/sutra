"""SQL injection detection — confirmed error-based + boolean-based differential.

check_sqli injects classic SQLi payloads into a target's parameters and confirms the bug two ways:
(1) ERROR-BASED — a payload provokes a database error string in the response that is NOT in the
clean baseline (MySQL/Postgres/MSSQL/Oracle/SQLite fingerprints) — a high-confidence confirmed find;
(2) BOOLEAN-BASED — a true condition (' AND '1'='1) returns the baseline page while a false one
(' AND '1'='2) returns a meaningfully different page — a strong candidate to verify. SQLi is a
top-payout class (data theft, auth bypass, sometimes RCE). Active (sends crafted requests) so it is
gated. Pure-stdlib (urllib), CI-safe, $0. Verify and scope before reporting; do NOT dump prod data.
"""

from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from friday.skills.registry import skill

# (database, error-string fingerprint). A match that is absent from the baseline = confirmed SQLi.
_ERROR_SIGS: list[tuple[str, re.Pattern[str]]] = [
    ("MySQL", re.compile(r"SQL syntax.*MySQL|Warning.*mysqli?|MySqlException|valid MySQL result|"
                         r"check the manual that corresponds to your (MySQL|MariaDB)", re.I)),
    ("PostgreSQL", re.compile(r"PostgreSQL.*ERROR|pg_query\(\)|PSQLException|Npgsql\.|"
                              r"unterminated quoted string at or near", re.I)),
    ("MSSQL", re.compile(r"Microsoft SQL Server|ODBC SQL Server Driver|SQLServerException|"
                         r"Unclosed quotation mark after the character string", re.I)),
    ("Oracle", re.compile(r"ORA-\d{5}|Oracle error|quoted string not properly terminated|"
                          r"OracleException", re.I)),
    ("SQLite", re.compile(r"SQLite/JDBCDriver|SQLite\.Exception|sqlite3\.OperationalError|"
                          r"unrecognized token|SQLITE_ERROR", re.I)),
]
_ERR_PAYLOADS = ("'", '"', "')", "';", "'\"", "`")
# (true-condition, false-condition) pairs — string-quoted and numeric.
_BOOL_PAIRS = (("' AND '1'='1", "' AND '1'='2"), (" AND 1=1", " AND 1=2"))
# Parameters that commonly carry a value that reaches a query.
_SQLI_PARAMS = frozenset(
    {"id", "page", "cat", "category", "user", "userid", "uid", "pid", "item", "product",
     "search", "q", "query", "name", "order", "sort", "year", "month", "view", "ref", "num"}
)
_DEFAULT_APPEND = ("id", "page", "q", "search", "cat", "user")


def _set_param(url: str, param: str, value: str) -> str:
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q[param] = value
    return urlunparse(parts._replace(query=urlencode(q)))


def _orig_value(url: str, param: str) -> str:
    for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True):
        if k == param:
            return v or "1"
    return "1"


def _probe(url: str) -> tuple[int, str, str | None]:
    req = urllib.request.Request(url, headers={"User-Agent": "sutra-sqli/1.0"})
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


def _db_error(body: str, baseline: str) -> tuple[str, str] | None:
    """Return (db, matched) if an error fingerprint is in body but not the clean baseline."""
    for db, pat in _ERROR_SIGS:
        m = pat.search(body or "")
        if m and not pat.search(baseline or ""):
            return db, m.group(0)[:120]
    return None


@skill(
    name="check_sqli",
    description=(
        "Test a URL for SQL injection — a top-payout class (data theft, auth bypass, sometimes "
        "RCE). Confirms two ways: error-based (a payload provokes a DB error string absent from "
        "the clean baseline — MySQL/Postgres/MSSQL/Oracle/SQLite) and boolean-based (a true "
        "condition returns the baseline page, a false one differs). Auto-targets id/page/search "
        "params or a small common set; pass param= for one. Active (crafted requests) — authorized "
        "targets only; verify and scope before reporting, never dump production data."
    ),
    auto_confirm=False,  # sends crafted SQL payloads — gated
    timeout_s=90,
)
async def check_sqli(url: str, param: str = "") -> dict[str, Any]:
    """Probe a URL's parameters for error-based and boolean-based SQL injection.

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
        candidates = [p for p in existing if p.lower() in _SQLI_PARAMS] or list(_DEFAULT_APPEND)

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

        # page-stability gate: a non-deterministic page makes boolean differentials meaningless,
        # so only run the boolean test when two clean baselines agree.
        boolean_ok = base_len > 0
        if boolean_ok:
            _, base2, be2 = _probe(url)
            if be2 is not None or not close(len(base2), base_len):
                boolean_ok = False
        for p in candidates:
            orig = _orig_value(url, p)
            # 1) error-based (confirmed)
            hit = False
            for payload in _ERR_PAYLOADS:
                status, body, err = _probe(_set_param(url, p, orig + payload))
                if err is None:
                    reachable = True
                got = _db_error(body, baseline)
                if got:
                    db, matched = got
                    findings.append({
                        "id": "sqli-error", "param": p, "severity": "high", "db": db,
                        "payload": orig + payload, "status": status, "evidence": matched,
                        "confidence": "confirmed",
                    })
                    hit = True
                    break
            if hit or not boolean_ok:
                continue
            # 2) boolean-based (candidate) — only on a stable page; re-confirmed to kill variance
            for t_cond, f_cond in _BOOL_PAIRS:
                _, t_body, te = _probe(_set_param(url, p, orig + t_cond))
                _, f_body, fe = _probe(_set_param(url, p, orig + f_cond))
                if te or fe:
                    continue
                tl, fl = len(t_body), len(f_body)
                if not (close(tl, base_len) and far(tl, fl)):
                    continue
                # re-request both to rule out a random fluke before flagging
                _, t2, te2 = _probe(_set_param(url, p, orig + t_cond))
                _, f2, fe2 = _probe(_set_param(url, p, orig + f_cond))
                if te2 or fe2 or not (close(len(t2), base_len) and far(len(t2), len(f2))):
                    continue
                findings.append({
                    "id": "sqli-boolean", "param": p, "severity": "high",
                    "payload": f"{orig}{t_cond} / {orig}{f_cond}", "confidence": "candidate",
                    "evidence": f"true≈base ({tl}≈{base_len}B), false≠ ({fl}B); reproduced",
                })
                break
        return findings, reachable

    findings, reachable = await asyncio.to_thread(_run)
    if not reachable:
        return {"error": f"could not reach {url}", "url": url}

    out_findings: list[dict[str, Any]] = []
    for f in findings:
        kind = f.get("db", "boolean")
        repro = _set_param(url, f["param"], _orig_value(url, f["param"]) + "'")
        out_findings.append({
            "id": f["id"],
            "severity": f["severity"],
            "matched_at": url,
            "name": f"SQLi ({kind}/{f['confidence']}) via '{f['param']}'",
            "description": (
                f"The '{f['param']}' parameter is injectable ({f['confidence']}): payload "
                f"'{f['payload']}' → {f['evidence']}. SQLi enables data theft, auth bypass and "
                "sometimes RCE — escalate read access to prove impact, never dump production data."
            ),
            "curl": f"curl -s '{repro}'",
        })

    return {
        "url": url,
        "tested_params": candidates,
        "findings": out_findings,
        "vulnerable": bool(out_findings),
        "note": (
            "Error-based hits are confirmed; boolean hits are strong candidates — verify with a "
            "manual differential / sqlmap before reporting, and never exfiltrate production data."
        ),
    }
