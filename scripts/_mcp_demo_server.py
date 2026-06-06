"""A tiny local MCP server for smoke-testing the bridge (stdio transport).

Run indirectly via scripts/mcp_smoke.py — not meant to be launched by hand.
Exposes one read-only tool (`add`) and one un-annotated tool (`shout`) so the
bridge's auto_confirm derivation can be observed end-to-end.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool(annotations={"readOnlyHint": True})
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
def shout(text: str) -> str:
    """Uppercase some text (no annotations → treated as untrusted)."""
    return text.upper()


if __name__ == "__main__":
    mcp.run(transport="stdio")
