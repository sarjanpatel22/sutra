"""Bug-bounty report drafting — the 'model reports' half of the money flow.

Turns REAL findings (from nuclei_scan / recon) into a submittable markdown report: title, severity,
affected URL, description, reproduction, impact, remediation, references. DETERMINISTIC — every
field comes straight from the tool's real output, so there is no room to fabricate. The operator
verifies and submits; this just assembles the hard-to-gather facts (the repro curl, the CWE/CVSS,
the references) into the standard structure. That's what turns a finding into a payout.
"""

from __future__ import annotations

from datetime import date
from typing import Any

_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "unknown": 5}


def _classification(f: dict[str, Any]) -> str:
    bits = [f"Severity: {(f.get('severity') or 'unknown').lower()}"]
    if f.get("cvss") is not None:
        bits.append(f"CVSS {f['cvss']}")
    cwe = ", ".join(f.get("cwe") or [])
    if cwe:
        bits.append(cwe.upper())
    return " · ".join(bits)


def _finding_section(f: dict[str, Any]) -> str:
    name = f.get("name") or f.get("id") or "Finding"
    sev = (f.get("severity") or "unknown").upper()
    url = f.get("matched_at") or ""
    out = [f"## [{sev}] {name}", "",
           f"- **Affected:** {url}",
           f"- **Classification:** {_classification(f)}",
           f"- **Detection:** nuclei template `{f.get('id', '')}`", ""]
    if f.get("description"):
        out += ["**Description**", "", str(f["description"]).strip(), ""]
    repro = f.get("curl") or (f"curl -i '{url}'" if url else "")
    if repro:
        out += ["**Steps to reproduce**", "", "```bash", repro.strip(), "```", ""]
    out += ["**Impact**", "",
            "> _Confirm the concrete impact on this target and the program's scope before "
            "submitting — do not submit an unverified or out-of-scope finding._", ""]
    out += ["**Remediation**", "",
            "Restrict or remove public access to the affected resource and confirm it is not "
            "exposed without authentication; see the references for class-specific guidance.", ""]
    refs = f.get("reference") or []
    if refs:
        out += ["**References**", "", *[f"- {r}" for r in refs], ""]
    return "\n".join(out)


def draft_bounty_report(target: str, findings: list[dict[str, Any]], *, program: str = "") -> str:
    """Assemble real findings into a submittable markdown report, worst-severity first. Pure
    formatting of the tool's real output — no invented facts."""
    findings = sorted(findings or [], key=lambda f: _SEV_ORDER.get(f.get("severity", "unknown"), 9))
    counts: dict[str, int] = {}
    for f in findings:
        s = (f.get("severity") or "unknown").lower()
        counts[s] = counts.get(s, 0) + 1
    summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items(),
                                                      key=lambda x: _SEV_ORDER.get(x[0], 9)))
    dateline = f"_Drafted {date.today().isoformat()}"
    dateline += (f" · program: {program}" if program else "") + "_"
    head = [
        f"# Bug-bounty report — {target}",
        dateline,
        "",
        f"**Findings:** {len(findings)}" + (f" ({summary})" if summary else ""),
        "",
        "> Drafted from real scanner output (nuclei). VERIFY each finding and its impact against "
        "the program scope before submitting — never submit unconfirmed or out-of-scope items.",
        "",
    ]
    if not findings:
        return "\n".join(head + ["_No findings to report._"])
    return "\n".join(head + ["---", ""] + [_finding_section(f) for f in findings])
