"""scan_injection skill — detect prompt-injection / exfiltration signatures in untrusted text.

Defensive + read-only: given a document, web page, email, or tool output, return a structured
verdict (injection yes/no, severity, categories, matched excerpts). Backs the `sutra scan-injection`
CLI and can pre-screen untrusted content inside the agent. Heuristic, no model call.
"""

from __future__ import annotations

from typing import Any

from friday.core import injection_guard
from friday.skills.registry import skill


@skill(
    name="scan_injection",
    description=(
        "Scan untrusted text (a document, web page, email, or tool output) for prompt-injection "
        "and data-exfiltration signatures — instruction override, system-prompt spoofing, role "
        "hijack, refusal suppression, prompt disclosure, secret exfiltration, tool hijack, "
        "egress-to-URL, encoded payloads. Returns a structured verdict (injection yes/no, "
        "severity, categories, matched excerpts). Read-only + heuristic (no model call) — a "
        "defensive check, safe to run on any content."
    ),
    auto_confirm=True,
)
async def scan_injection(text: str = "") -> dict[str, Any]:
    """Verdict for `text`: {injection, severity, score, categories, detections}. See
    friday.core.injection_guard.verdict."""
    return injection_guard.verdict(text or "")
