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
    urls = f.get("affected") or ([f["matched_at"]] if f.get("matched_at") else [])
    url = urls[0] if urls else ""
    out = [f"## [{sev}] {name}", "",
           f"- **Affected:** {', '.join(urls) if urls else '(see template)'}",
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


_PRIORITY = {"critical", "high", "medium"}


def _dedup(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge findings of the same template (fired across multiple hosts) into one entry listing
    every affected URL — so a hunt shows '1 finding · 5 hosts', not 5 duplicate sections."""
    groups: dict[str, dict[str, Any]] = {}
    for f in findings:
        key = str(f.get("id") or f.get("name") or id(f))
        g = groups.get(key)
        if g is None:
            g = dict(f)
            g["affected"] = []
            groups[key] = g
        url = f.get("matched_at") or ""
        if url and url not in g["affected"]:
            g["affected"].append(url)
    return list(groups.values())


def draft_bounty_report(target: str, findings: list[dict[str, Any]], *, program: str = "",
                        warnings: list[str] | None = None,
                        surfaces: list[str] | None = None) -> str:
    """Assemble real findings into a submittable, TRIAGED markdown report — deduped across hosts,
    submit-worthy (medium+) findings first, informational noise condensed below. Pure formatting of
    the tool's real output — no invented facts. `warnings` (incomplete scans, failed enum) are
    surfaced up top so '0 findings' is never mistaken for 'clean'. `surfaces` (the live hosts) feed
    the coach — a deterministic manual-test plan appended at the end (the scanner-blind moves)."""
    deduped = sorted(_dedup(findings or []),
                     key=lambda f: _SEV_ORDER.get((f.get("severity") or "unknown").lower(), 9))
    priority = [f for f in deduped if (f.get("severity") or "").lower() in _PRIORITY]
    info = [f for f in deduped if f not in priority]
    dateline = f"_Drafted {date.today().isoformat()}"
    dateline += (f" · program: {program}" if program else "") + "_"
    head = [
        f"# Bug-bounty report — {target}",
        dateline,
        "",
        f"**Findings:** {len(deduped)} — {len(priority)} submit-worthy, {len(info)} informational",
        "",
        "> Drafted from real scanner output (nuclei). VERIFY each finding and its impact against "
        "the program scope before submitting — never submit unconfirmed or out-of-scope items.",
        "",
    ]
    if warnings:
        head += ["> ⚠️ **Scan was INCOMPLETE — '0 findings' here does NOT mean 'clean'. Re-run "
                 "before trusting it:**", "", *[f"> - {w}" for w in warnings], ""]
    # the coach: a deterministic manual-test plan from the real surfaces (scanner-blind moves)
    plan = ""
    if deduped or surfaces:
        from friday.core.coach import render_plan, suggest_tests
        plan = "\n\n" + render_plan(suggest_tests(deduped, surfaces))
    if not deduped:
        return "\n".join(head + ["_No findings to report._"]) + plan
    body: list[str] = []
    if priority:
        body += ["## Submit-worthy findings (triage these first)", ""]
        body += [_finding_section(f) for f in priority]
    if info:
        body += ["## Informational / lower priority", "",
                 "_Usually low or no bounty — submit only if the program rewards this class:_", ""]
        for f in info:
            n = len(f.get("affected") or [])
            host_note = f" · {n} hosts" if n > 1 else ""
            body.append(f"- **[{(f.get('severity') or '?').upper()}]** "
                        f"{f.get('name') or f.get('id')}{host_note} — `{f.get('id', '')}`")
        body.append("")
    return "\n".join(head + ["---", ""] + body) + plan
