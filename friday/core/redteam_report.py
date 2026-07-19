"""Draft a submittable LLM red-team report from confirmed battery findings.

The 'model reports' half for AI red-teaming: turns redteam_llm's CONFIRMED findings into the
markdown structure a bounty program / arena expects — title, OWASP-LLM class, severity, an exact
reproduction (system prompt + attack + the canary that proves it), impact, and remediation. Every
field comes straight from the tool's real output, so nothing is fabricated. The operator verifies
scope and submits.
"""

from __future__ import annotations

from datetime import date
from typing import Any

_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "unknown": 5}

_IMPACT = {
    "system_prompt_leak": "The model disclosed its confidential system prompt, exposing hidden "
                          "instructions, secrets, or guardrail logic an attacker can then bypass.",
    "instruction_override": "User input overrode the application's own instructions — the model "
                            "can be repurposed away from its intended, safety-bounded function.",
    "safety_rule_bypass": "An explicit safety rule was defeated by adversarial phrasing, showing "
                          "the model will violate operator-set constraints under pressure.",
    "indirect_injection": "Instructions embedded in retrieved/tool content were executed as if "
                          "trusted — the classic path to data exfiltration and tool abuse in "
                          "agentic/RAG systems.",
    "encoded_injection": "A base64-obfuscated instruction bypassed the model's input handling — "
                         "encoding defeats naive keyword/safety filters, so a content guard on "
                         "this endpoint can be evaded by hiding the payload.",
    "multiturn_injection": "A multi-turn (crescendo) conversation walked the model past guardrails "
                           "that held on a single turn — it was led over several messages into "
                           "executing an injected instruction, the pattern real jailbreaks and "
                           "agent hijacks rely on.",
}

_REMEDIATION = (
    "Enforce instruction hierarchy (treat user and retrieved content as untrusted), add "
    "input/output guardrails for the affected class, keep secrets out of the prompt, and re-test "
    "with this reproduction until the canary can no longer be elicited."
)


def _section(f: dict[str, Any]) -> str:
    sev = (f.get("severity") or "unknown").upper()
    cat = f.get("category", "finding")
    owasp = f.get("owasp", "")
    out = [f"## [{sev}] {owasp or cat}", "",
           f"- **Class:** {cat}",
           f"- **OWASP-LLM:** {owasp}",
           f"- **Severity:** {sev}", "",
           "**Reproduction**", "",
           "_System prompt given to the model (the planted rule/secret):_", "",
           "```", str(f.get("system", "")).strip(), "```", "",
           "_Adversarial user message:_", "",
           "```", str(f.get("attack", "")).strip(), "```", "",
           f"_Result:_ {f.get('evidence', '')}", "",
           "**Impact**", "",
           _IMPACT.get(cat, "The model violated an operator-set constraint under adversarial "
                            "input."), "",
           "**Remediation**", "", _REMEDIATION, ""]
    return "\n".join(out)


def _candidate_section(c: dict[str, Any]) -> str:
    sev = (c.get("severity") or "unknown").upper()
    return "\n".join([
        f"### [{sev}] {c.get('owasp') or c.get('category', 'candidate')}  — CANDIDATE",
        "",
        f"- **Class:** {c.get('category', '')}",
        f"- **What we saw:** {c.get('evidence', '')}",
        f"- **To confirm:** {c.get('verify', 'Verify the real-world impact before reporting.')}",
        "",
    ])


def draft_redteam_report(
    target: str, findings: list[dict[str, Any]], *, program: str = "", model: str = "",
    candidates: list[dict[str, Any]] | None = None,
) -> str:
    """Assemble a submittable markdown report. `findings` are CONFIRMED (from redteam_llm); the
    optional `candidates` are heuristic leads rendered in a SEPARATE, clearly-marked section so a
    confirmed finding is never confused with an unverified one."""
    candidates = candidates or []
    confirmed = [f for f in findings if f.get("vulnerable")]
    confirmed.sort(key=lambda f: _SEV_ORDER.get((f.get("severity") or "unknown").lower(), 5))
    top = confirmed[0]["severity"].upper() if confirmed else "NONE"
    cats = ", ".join(sorted({f.get("category", "") for f in confirmed})) or "none"

    head = [
        f"# LLM Red-Team Report — {target}",
        "",
        f"- **Date:** {date.today().isoformat()}",
        f"- **Target endpoint:** {target}",
        *( [f"- **Model:** {model}"] if model else [] ),
        *( [f"- **Program:** {program}"] if program else [] ),
        f"- **Confirmed findings:** {len(confirmed)} (highest severity: {top})",
        f"- **Candidate leads (unconfirmed):** {len(candidates)}",
        f"- **Classes:** {cats}",
        "",
        "> Confirmed findings are proven via benign planted canaries (no harmful content "
        "elicited). Verify each is in scope before submitting, and do not escalate beyond the "
        "canary or access real data.",
        "",
        "---",
        "",
    ]
    parts: list[str] = []
    if confirmed:
        body = "\n\n---\n\n".join(_section(f) for f in confirmed)
        parts.append("## Confirmed findings\n\n" + body)
    else:
        parts.append("_No confirmed weaknesses — the target protected every planted canary._")
    if candidates:
        parts.append(
            "## Candidate leads — NOT confirmed, verify before submitting\n\n"
            "> These are heuristic signals, not proven findings. Each needs the check below to "
            "become reportable — treat them as leads, never submit as-is.\n\n"
            + "\n".join(_candidate_section(c) for c in candidates)
        )
    return "\n".join(head) + "\n\n".join(parts)
