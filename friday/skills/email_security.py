"""Email-spoofing exposure — missing/weak SPF and DMARC let anyone spoof mail from the domain.

check_email_spoofing reads the domain's SPF (TXT) and DMARC (_dmarc TXT) records via dig and flags
spoofable states: no SPF, permissive SPF (+all/?all), no DMARC, or DMARC p=none (monitor only, does
not block). Passive DNS lookups; degrades gracefully if dig is absent. CI-safe, $0.
"""

from __future__ import annotations

import shutil
from typing import Any

from friday.skills.recon import _run  # shared async subprocess helper (returncode, stdout, stderr)
from friday.skills.registry import skill


async def _txt(name: str) -> list[str]:
    """TXT records for `name` via dig (+short), quotes stripped. Empty if dig is absent or none."""
    dig = shutil.which("dig")
    if not dig:
        return []
    _, out, _ = await _run([dig, "+short", "TXT", name], timeout=10)
    return [ln.strip().strip('"') for ln in (out or "").splitlines() if ln.strip()]


def _spf_all_qualifier(spf: str) -> str:
    """Qualifier on the SPF 'all' mechanism: one of + - ~ ? (a bare 'all' defaults to '+'),
    or '' if there is no 'all' mechanism. Tokenized so it can't match inside an include/macro."""
    for tok in spf.split():
        t = tok.strip().lower()
        if t == "all":
            return "+"
        if t[1:] == "all" and t[:1] in "+-~?":
            return t[0]
    return ""


def _dmarc_tag(record: str, tag: str) -> str:
    """Value of a specific DMARC tag (e.g. 'p'), matched EXACTLY so 'p' is never confused with
    'sp' (subdomain policy) — the substring trap that mislabels p=reject;sp=none as monitor-only."""
    for part in record.split(";"):
        k, sep, v = part.partition("=")
        if sep and k.strip().lower() == tag:
            return v.strip().lower()
    return ""


@skill(
    name="check_email_spoofing",
    description=(
        "Check a domain's email anti-spoofing posture — reads SPF (TXT) and DMARC (_dmarc TXT) and "
        "flags spoofable states: no SPF, a permissive SPF (+all/?all), no DMARC, or DMARC p=none "
        "(monitor only, doesn't block). Missing/weak records let anyone send mail as the domain "
        "(phishing). Passive DNS lookups. Confirm with a spoofed test mail before reporting."
    ),
    auto_confirm=True,  # passive DNS lookups (dig TXT) — auto
    timeout_s=30,
)
async def check_email_spoofing(domain: str) -> dict[str, Any]:
    """Read a domain's SPF/DMARC and flag spoofable misconfigurations.

    domain: the domain to check (a URL or host is accepted; the hostname is used).
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return {"error": "no domain to check"}
    if "://" in domain:
        from urllib.parse import urlparse
        domain = urlparse(domain).hostname or domain
    domain = domain.split("/")[0].split(":")[0]

    spf = next((t for t in await _txt(domain) if t.lower().startswith("v=spf1")), "")
    dmarc_txt = await _txt(f"_dmarc.{domain}")
    dmarc = next((t for t in dmarc_txt if t.lower().startswith("v=dmarc1")), "")

    findings: list[dict[str, Any]] = []
    if not spf:
        findings.append({
            "id": "spf-missing", "severity": "low", "matched_at": domain, "name": "No SPF record",
            "description": f"{domain} has no SPF (v=spf1) TXT record — no sender policy.",
        })
    elif _spf_all_qualifier(spf) in ("+", "?"):
        findings.append({
            "id": "spf-weak", "severity": "medium", "matched_at": domain,
            "name": "Permissive SPF (+all/?all)",
            "description": (f"SPF '{spf}' allows any sender (+all/?all) — no protection. "
                            "It should end in -all (hard fail) or ~all (soft fail)."),
        })
    if not dmarc:
        findings.append({
            "id": "dmarc-missing", "severity": "medium", "matched_at": domain,
            "name": "No DMARC record",
            "description": (f"{domain} has no DMARC record — receivers won't reject spoofed "
                            "mail claiming to be from this domain."),
        })
    elif _dmarc_tag(dmarc, "p") == "none":
        findings.append({
            "id": "dmarc-none", "severity": "low", "matched_at": domain,
            "name": "DMARC policy p=none (monitor only)",
            "description": (f"DMARC '{dmarc}' uses p=none — it monitors but does NOT block spoofed "
                            "mail. Move to p=quarantine or p=reject."),
        })

    return {
        "domain": domain,
        "spf": spf,
        "dmarc": dmarc,
        "findings": findings,
        "vulnerable": any(f["severity"] in ("medium", "high") for f in findings),
        "note": ("Confirm by sending a spoofed email to a mailbox you control before reporting; "
                 "some programs treat email spoofing as out of scope."),
    }
