"""The active coach — turn real hunt findings into a MANUAL test plan.

Tools find the surface (nuclei + content discovery); this maps each real surface to the manual
tests a scanner can't run (IDOR, SSRF, auth/logic, stored XSS) and points at the knowledge doc that
teaches how. DETERMINISTIC — it only maps real URLs to a fixed ruleset, so it never invents a
finding; it suggests what a human should verify next. That's the tools-find → coach-guides →
you-exploit loop (see docs/knowledge/bb_recon.md). The model isn't in the loop here, so it can't
hallucinate progress.
"""

from __future__ import annotations

from typing import Any

# (url/finding keywords, vuln class, the concrete manual move + the doc to read). Order = priority.
_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("graphql", "graphiql"), "GraphQL",
     "Try `__schema` introspection → map mutations for missing auth, IDOR on node(id:) "
     "(bb_recon, bb_access_control)"),
    (("/api", "/v1/", "/v2/", "/rest/", "swagger", "openapi", "api-docs"), "Access control",
     "Enumerate endpoints from the API doc; swap object ids (IDOR) + call admin routes with a "
     "low-priv token (BFLA) (bb_access_control)"),
    (("url=", "redirect", "callback", "next=", "dest=", "feed=", "webhook", "import"),
     "SSRF / open redirect",
     "Point the param at a collaborator + 169.254.169.254 (SSRF→cloud creds); test open redirect "
     "(bb_server_side)"),
    (("admin", "dashboard", "panel", "console", "/manage", "internal"), "Access control / auth",
     "Reach it as anon/low-priv (BFLA); try default + weak creds (bb_access_control)"),
    (("login", "/auth", "oauth", "/sso", "reset", "password", "register", "/2fa", "otp"), "Auth",
     "Test reset/OTP/2FA/OAuth flaws — host-header poisoning, no rate-limit brute, redirect_uri, "
     "JWT alg/secret (bb_auth_logic)"),
    (("upload", "/file", "avatar", "/image", "attachment"), "Upload",
     "Bypass file-type checks; SSRF via URL-import; stored XSS in filename/metadata "
     "(bb_server_side, bb_client_side)"),
    (("search", "q=", "query", "filter", "sort", "id="), "Injection / IDOR",
     "Test SQLi (boolean/time-based) + reflected XSS; swap the id (IDOR) (bb_server_side, "
     "bb_access_control)"),
    ((".git", ".env", "backup", ".sql", ".bak", "/config", "dump", "credential", "secret"),
     "Exposure",
     "Confirm it serves real secrets/data; if live, escalate to what the secret unlocks "
     "(bb_high_value)"),
    (("/ftp", "/files", "/download", "/export", "/invoice", "/report"), "Exposure / IDOR",
     "List the directory; fetch other users' files by changing the id (IDOR) (bb_access_control)"),
    (("comment", "/post", "profile", "message", "/ticket", "review", "/name"), "Stored XSS / logic",
     "Plant stored XSS an admin/other user will render; test the workflow for logic flaws "
     "(bb_client_side, bb_auth_logic)"),
    (("cart", "checkout", "/pay", "order", "balance", "coupon", "price", "transfer"),
     "Business logic",
     "Race the request (parallel) for double-spend; tamper price/quantity/status (bb_auth_logic)"),
]

# Always worth doing on any live host — the recon depth that surfaces the un-duped stuff.
_ALWAYS: list[tuple[str, str]] = [
    ("Recon depth", "Pull every JS file → grep for endpoints + hardcoded secrets/keys; mine hidden "
                    "params (Arjun/x8); check wayback for dead-but-live endpoints (bb_recon)"),
    ("Access control", "Make two accounts and replay every state-changing request cross-account — "
                       "the #1 payable, scanner-blind bug (bb_access_control)"),
]


def suggest_tests(findings: list[dict[str, Any]], surfaces: list[str] | None = None,
                  *, cap: int = 12) -> list[dict[str, str]]:
    """Map real finding URLs + live hosts to the manual tests worth running next. Deterministic +
    deduped by (surface, class). Returns [{surface, cls, test}] — verify each by hand."""
    urls: list[str] = []
    for f in findings or []:
        for u in (f.get("affected") or ([f["matched_at"]] if f.get("matched_at") else [])):
            urls.append(u)
    urls += surfaces or []
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for url in urls:
        low = (url or "").lower()
        for keys, cls, test in _RULES:
            if any(k in low for k in keys) and (url, cls) not in seen:
                seen.add((url, cls))
                out.append({"surface": url, "cls": cls, "test": test})
                if len(out) >= cap:
                    return out
    return out


def render_plan(suggestions: list[dict[str, str]]) -> str:
    """Markdown 'manual tests to try next' section — the coached worklist appended to a report."""
    lines = ["## Manual tests to try next (coach)", "",
             "_Scanners can't find these — verify each by hand, with your own accounts, in scope:_",
             ""]
    for s in suggestions:
        lines.append(f"- **{s['surface']}** → _{s['cls']}_: {s['test']}")
    lines += ["", "**On every live host:**"]
    lines += [f"- _{cls}_: {test}" for cls, test in _ALWAYS]
    return "\n".join(lines)
