"""mcp_audit skill — static vulnerability triage for an MCP server / agent-tool checkout.

Defensive + read-only: scan a directory of source for the sink patterns that dominate the MCP CVE
wave (command injection, path traversal, SSRF, unsafe deserialization, code eval) and return a
ranked, structured report. Heuristic (no model call) — the findings are CANDIDATES a human verifies
against the real tool wiring, then discloses responsibly. Backs the `sutra mcp-audit` CLI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from friday.core import mcp_audit
from friday.skills.registry import skill


@skill(
    name="mcp_audit",
    description=(
        "Statically scan an MCP server / agent-tool source directory for the vulnerability classes "
        "that dominate the MCP CVE wave: command injection (CWE-78), SQL injection (CWE-89), path "
        "traversal (CWE-22), SSRF (CWE-918), unsafe deserialization (CWE-502), code eval (CWE-95). "
        "Returns a ranked, "
        "structured report (file, line, category, CWE, severity, reachable, why) of CANDIDATE "
        "sinks to verify before responsible disclosure. `reachable` findings are fed by an MCP "
        "tool parameter (attacker-influenced) — set reachable_only=True to see just those, the "
        "ones most likely to be real. Read-only + heuristic (no model call) — safe on any "
        "checkout. Give it a local path to a cloned repo."
    ),
    auto_confirm=True,
)
async def mcp_audit_skill(
    path: str = ".", limit: int = 40, reachable_only: bool = False,
) -> dict[str, Any]:
    """Scan `path` (a local source directory) and return the audit report. `limit` caps the number
    of findings returned (0 = all); the report's counts always reflect the full scan.
    `reachable_only` keeps only sinks fed by a tool-handler parameter (attacker-reachable)."""
    root = Path(path).expanduser()
    if not root.exists():
        return {"error": f"no such path: {path}", "findings": []}
    report = mcp_audit.audit_report(root)
    findings = report.get("findings")
    if isinstance(findings, list):
        if reachable_only:
            findings = [f for f in findings if f.get("reachable")]
            report["findings"] = findings
        if limit:
            report["findings"] = findings[:limit]
        report["shown"] = len(report["findings"])
    return report
