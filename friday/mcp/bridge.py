"""MCP host/client bridge — connect to servers, surface their tools as skills."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from friday.skills import registry

log = logging.getLogger("sutra.mcp")

# ── config ──────────────────────────────────────────────────────────────────
# Claude-Desktop-compatible: {"mcpServers": {"name": {"command", "args", "env"}}}.
# Searched in order: $SUTRA_MCP_CONFIG, ~/.config/sutra/, repo root.


def _config_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.getenv("SUTRA_MCP_CONFIG")
    if env:
        paths.append(Path(env).expanduser())
    paths.append(Path.home() / ".config" / "sutra" / "mcp_servers.json")
    paths.append(Path(__file__).resolve().parents[2] / "mcp_servers.json")
    return paths


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, dict[str, Any]]:
    """Load the MCP server map. Accepts either a {"mcpServers": {...}} wrapper or a
    bare {name: spec} map. Returns {} (not an error) when no config exists."""
    candidates = [Path(path)] if path else _config_paths()
    for p in candidates:
        try:
            if p.is_file():
                data = json.loads(p.read_text())
                servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
                return {k: v for k, v in servers.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError) as e:
            log.warning("ignoring bad MCP config %s: %s", p, e)
    return {}


def mcp_configured() -> bool:
    """True if at least one (non-disabled) MCP server is configured."""
    return any(not spec.get("disabled") for spec in load_config().values())


# ── helpers (pure — unit-tested without spawning servers) ────────────────────


def _sanitize(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)


def skill_name(server: str, tool: str) -> str:
    """Namespaced, function-name-safe skill id for an MCP tool (<=64 chars)."""
    return f"mcp__{_sanitize(server)}__{_sanitize(tool)}"[:64]


def to_openai_schema(
    name: str, description: str, input_schema: dict[str, Any] | None
) -> dict[str, Any]:
    """Wrap an MCP tool's JSON-Schema inputs into an OpenAI function-tool schema."""
    params = dict(input_schema) if isinstance(input_schema, dict) and input_schema else {}
    params.setdefault("type", "object")
    params.setdefault("properties", {})
    return {
        "type": "function",
        "function": {"name": name, "description": description or "", "parameters": params},
    }


def is_read_only(tool: Any) -> bool:
    """Whether a tool is safe to auto-run. Untrusted-by-default: only the server's
    own `readOnlyHint` annotation lets a tool skip the confirmation gate."""
    ann = getattr(tool, "annotations", None)
    return bool(getattr(ann, "readOnlyHint", False)) if ann is not None else False


def extract_result(result: Any) -> Any:
    """Flatten a CallToolResult into something JSON-serialisable for the tool reply."""
    if getattr(result, "isError", False):
        return {"error": _text(result) or "MCP tool returned an error"}
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    return _text(result)


def _text(result: Any) -> str:
    parts = [
        block.text
        for block in (getattr(result, "content", None) or [])
        if getattr(block, "text", None)
    ]
    return "\n".join(parts)


# ── bridge ───────────────────────────────────────────────────────────────────


class MCPBridge:
    """Holds open sessions to MCP servers and mirrors their tools into the registry."""

    def __init__(self, config: dict[str, dict[str, Any]] | None = None) -> None:
        self._config = config
        self._stack = AsyncExitStack()
        self._sessions: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}  # one per server — calls are serialized
        self._tool_names: list[str] = []

    @property
    def tool_names(self) -> list[str]:
        return list(self._tool_names)

    @property
    def servers(self) -> list[str]:
        return list(self._sessions)

    async def connect(self) -> MCPBridge:
        """Start every configured server. A server that fails is logged and skipped
        — one bad server never takes down the others (or the agent)."""
        config = self._config if self._config is not None else load_config()
        for name, spec in config.items():
            if spec.get("disabled"):
                continue
            try:
                await self._connect_one(name, spec)
            except Exception as e:  # noqa: BLE001 — degrade, don't crash the agent
                log.warning("MCP server %r failed to start: %s", name, e)
        return self

    async def _connect_one(self, name: str, spec: dict[str, Any]) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=spec["command"],
            args=spec.get("args", []),
            env=spec.get("env") or None,
            cwd=spec.get("cwd"),
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._sessions[name] = session

        tools = (await session.list_tools()).tools
        for tool in tools:
            self._register(name, session, tool)
        log.info("MCP %r: %d tool(s) registered", name, len(tools))

    def _register(self, server: str, session: Any, tool: Any) -> None:
        sname = skill_name(server, tool.name)
        schema = to_openai_schema(sname, tool.description or "", tool.inputSchema)
        tool_name = tool.name  # bind for the closure
        # One lock per server: a ClientSession multiplexes a single stdio stream and
        # is not safe for concurrent call_tool, but the agent runs tools in parallel.
        lock = self._locks.setdefault(server, asyncio.Lock())

        async def _invoke(**kwargs: Any) -> Any:
            async with lock:
                result = await session.call_tool(tool_name, kwargs or None)
            return extract_result(result)

        registry.register_external(
            sname,
            tool.description or "",
            _invoke,
            schema=schema,
            auto_confirm=is_read_only(tool),
            timeout_s=60,
        )
        self._tool_names.append(sname)

    async def aclose(self) -> None:
        """Unregister mirrored tools and close all server subprocesses."""
        for n in self._tool_names:
            registry.unregister(n)
        self._tool_names.clear()
        self._sessions.clear()
        self._locks.clear()
        await self._stack.aclose()


# Process-wide singleton — connect lazily, only when MCP is actually configured.
_bridge: MCPBridge | None = None
# Guards check-and-connect so two concurrent callers (e.g. chat loop + dashboard)
# can't both spawn a bridge — that would orphan subprocesses and double-register tools.
_bridge_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _bridge_lock
    if _bridge_lock is None:
        _bridge_lock = asyncio.Lock()
    return _bridge_lock


async def get_bridge() -> MCPBridge | None:
    """Return the connected bridge, starting it on first use. Returns None when no
    MCP server is configured, so callers can `if await get_bridge()` cheaply."""
    global _bridge
    if _bridge is not None:  # fast path — already connected, no lock needed
        return _bridge
    async with _get_lock():
        if _bridge is not None:  # another coroutine connected while we waited
            return _bridge
        if not mcp_configured():
            return None
        _bridge = await MCPBridge().connect()
        return _bridge


async def close_bridge() -> None:
    global _bridge
    async with _get_lock():
        if _bridge is not None:
            await _bridge.aclose()
            _bridge = None
