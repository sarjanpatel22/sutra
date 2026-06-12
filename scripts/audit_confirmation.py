"""Audit the confirmation gate: no irreversible skill may run without confirmation.

Enforcement — the agent declines auto_confirm=False skills unless approved — is
tested in tests/test_agent.py. This checks the *classification* side: that no skill
which mutates state ships auto-confirmed, across the live registry and (with --mcp)
any connected off-the-shelf MCP tools. Prints the full gate posture and exits
non-zero on any violation, so it doubles as a CI guard and living documentation of
the v1 "confirmation false-negative rate = 0%" criterion.

    python scripts/audit_confirmation.py          # first-party skills
    python scripts/audit_confirmation.py --mcp     # also connect + audit MCP tools
"""

from __future__ import annotations

import argparse
import asyncio

from friday.skills import audit, registry


async def main() -> int:
    ap = argparse.ArgumentParser(description="Audit the confirmation gate.")
    ap.add_argument(
        "--mcp", action="store_true", help="connect MCP servers and audit their tools too"
    )
    args = ap.parse_args()

    registry.autodiscover()
    if args.mcp:
        try:
            from friday.mcp.bridge import get_bridge

            await get_bridge()  # registers mcp__* tools (auto_confirm=is_read_only)
        except Exception as e:  # noqa: BLE001 — audit first-party even if MCP is down
            print(f"(MCP not audited: {e})\n")

    metas = [registry.get(n) for n in registry.list_skills()]
    print(audit.report(metas))

    problems = audit.all_problems(metas)
    if problems:
        print("\n✗ confirmation audit FAILED — confirmation false-negative risk:")
        for p in problems:
            print(f"    ✗ {p}")
        return 1
    gated = sorted(m.name for m in metas if not m.auto_confirm)
    print(f"\n✓ confirmation audit PASSED — {len(gated)} gated skill(s): {', '.join(gated)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
