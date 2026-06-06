"""MCP (Model Context Protocol) host — Sutra speaks MCP to off-the-shelf servers.

The bridge connects to configured MCP servers, lists their tools, and registers
each one into the existing `@skill` registry (namespaced `mcp__server__tool`), so
the agent tool-loop calls them exactly like first-party skills. Additive: nothing
in the agent changes. Local `@skill` stays for first-party/latency-critical work.
"""

from friday.mcp.bridge import MCPBridge, get_bridge, load_config, mcp_configured

__all__ = ["MCPBridge", "get_bridge", "load_config", "mcp_configured"]
