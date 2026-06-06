"""End-to-end MCP bridge smoke test against a real stdio server.

Launches scripts/_mcp_demo_server.py as an MCP server over stdio, connects the
bridge, mirrors its tools into the skill registry, and calls them through the
exact same path the agent loop uses (registry.call). No network, no npx.

    python scripts/mcp_smoke.py
"""

import asyncio
import sys
from pathlib import Path

from friday.mcp.bridge import MCPBridge
from friday.skills import registry

SERVER = str(Path(__file__).parent / "_mcp_demo_server.py")


async def main() -> int:
    config = {"demo": {"command": sys.executable, "args": [SERVER]}}
    bridge = await MCPBridge(config).connect()
    try:
        if not bridge.servers:
            print("FAIL: demo server did not come up")
            return 1
        print(f"connected: {bridge.servers}")
        print(f"tools:     {bridge.tool_names}")

        # Gate derivation: add() is readOnly → auto; shout() un-annotated → confirm.
        add_meta = registry.get("mcp__demo__add")
        shout_meta = registry.get("mcp__demo__shout")
        print(f"mcp__demo__add  auto_confirm={add_meta.auto_confirm}  (expect True)")
        print(f"mcp__demo__shout auto_confirm={shout_meta.auto_confirm} (expect False)")

        # Call both through the registry — the agent's real invocation path.
        s = await registry.call("mcp__demo__add", a=2, b=40)
        h = await registry.call("mcp__demo__shout", text="it works")
        print(f"add(2,40)      -> {s!r}")
        print(f"shout('it works') -> {h!r}")

        # MCP servers may return a scalar as text or wrap it in structuredContent
        # ({"result": ...}); the bridge surfaces whichever the server sent.
        ok = (
            "mcp__demo__add" in bridge.tool_names
            and add_meta.auto_confirm is True
            and shout_meta.auto_confirm is False
            and "42" in str(s)
            and "IT WORKS" in str(h)
        )
        print("\nRESULT:", "PASS ✅" if ok else "FAIL ❌")
        return 0 if ok else 1
    finally:
        await bridge.aclose()
        # Confirm teardown unregistered the mirrored tools.
        leaked = [n for n in registry.list_skills() if n.startswith("mcp__demo__")]
        print("post-close leaked tools:", leaked or "none")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
