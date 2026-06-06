"""Tests for the MCP bridge — config, schema mapping, and registry mirroring.

No real servers are spawned: the bridge's pure helpers and its registration path
(via a fake session) are exercised directly.
"""

import json

from friday.mcp import bridge
from friday.mcp.bridge import (
    MCPBridge,
    extract_result,
    is_read_only,
    load_config,
    mcp_configured,
    skill_name,
    to_openai_schema,
)
from friday.skills import registry


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeAnnotations:
    def __init__(self, read_only=False):
        self.readOnlyHint = read_only


class FakeTool:
    def __init__(self, name, description="", input_schema=None, annotations=None):
        self.name = name
        self.description = description
        self.inputSchema = input_schema
        self.annotations = annotations


class FakeContent:
    def __init__(self, text):
        self.text = text


class FakeResult:
    def __init__(self, content=None, structured=None, is_error=False):
        self.content = content or []
        self.structuredContent = structured
        self.isError = is_error


class FakeSession:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def call_tool(self, name, args=None):
        self.calls.append((name, args))
        return self._result


# ── pure helpers ─────────────────────────────────────────────────────────────
def test_skill_name_sanitizes_and_caps():
    assert skill_name("fs", "read_file") == "mcp__fs__read_file"
    assert skill_name("we!rd", "to ol") == "mcp__we_rd__to_ol"
    assert len(skill_name("s" * 50, "t" * 50)) <= 64


def test_to_openai_schema_fills_defaults():
    s = to_openai_schema("mcp__x__y", "does y", None)
    assert s["type"] == "function"
    assert s["function"]["name"] == "mcp__x__y"
    assert s["function"]["parameters"] == {"type": "object", "properties": {}}


def test_to_openai_schema_passthrough():
    inp = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    s = to_openai_schema("n", "d", inp)
    assert s["function"]["parameters"]["required"] == ["path"]


def test_is_read_only_untrusted_by_default():
    assert is_read_only(FakeTool("t")) is False  # no annotations → gated
    assert is_read_only(FakeTool("t", annotations=FakeAnnotations(read_only=False))) is False
    assert is_read_only(FakeTool("t", annotations=FakeAnnotations(read_only=True))) is True


def test_extract_result_text_structured_and_error():
    assert extract_result(FakeResult(content=[FakeContent("a"), FakeContent("b")])) == "a\nb"
    assert extract_result(FakeResult(structured={"k": 1})) == {"k": 1}
    err = extract_result(FakeResult(content=[FakeContent("boom")], is_error=True))
    assert err == {"error": "boom"}


# ── config ───────────────────────────────────────────────────────────────────
def test_load_config_wrapper_and_bare(tmp_path):
    wrapped = tmp_path / "w.json"
    wrapped.write_text(json.dumps({"mcpServers": {"git": {"command": "x"}}}))
    assert load_config(wrapped) == {"git": {"command": "x"}}

    bare = tmp_path / "b.json"
    bare.write_text(json.dumps({"git": {"command": "y"}}))
    assert load_config(bare) == {"git": {"command": "y"}}


def test_load_config_missing_is_empty(tmp_path):
    assert load_config(tmp_path / "nope.json") == {}


def test_load_config_bad_json_ignored(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_config(bad) == {}


def test_mcp_configured_respects_disabled(tmp_path, monkeypatch):
    cfg = tmp_path / "mcp_servers.json"
    cfg.write_text(json.dumps({"mcpServers": {"a": {"command": "x", "disabled": True}}}))
    monkeypatch.setenv("SUTRA_MCP_CONFIG", str(cfg))
    assert mcp_configured() is False
    cfg.write_text(json.dumps({"mcpServers": {"a": {"command": "x"}}}))
    assert mcp_configured() is True


# ── registry mirroring (the core bridge behaviour) ───────────────────────────
async def test_register_mirrors_tool_and_invokes():
    b = MCPBridge()
    session = FakeSession(FakeResult(content=[FakeContent("hello")]))
    tool = FakeTool(
        "read_file",
        "reads a file",
        {"type": "object", "properties": {"path": {"type": "string"}}},
        FakeAnnotations(read_only=True),
    )
    b._register("fstest", session, tool)
    name = "mcp__fstest__read_file"
    try:
        assert name in registry.list_skills()
        meta = registry.get(name)
        assert meta.auto_confirm is True  # readOnly tools may auto-run
        assert {"function": {"name": name}}  # schema present
        assert any(t["function"]["name"] == name for t in registry.all_tools())

        out = await registry.call(name, path="/etc/hosts")
        assert out == "hello"
        assert session.calls == [("read_file", {"path": "/etc/hosts"})]
    finally:
        await b.aclose()
    assert name not in registry.list_skills()  # teardown unregisters


async def test_write_tool_is_gated_by_default():
    b = MCPBridge()
    tool = FakeTool("write_file", "writes", {"type": "object", "properties": {}})  # no annotations
    b._register("fstest", FakeSession(FakeResult()), tool)
    try:
        assert registry.get("mcp__fstest__write_file").auto_confirm is False
    finally:
        await b.aclose()


async def test_get_bridge_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(bridge, "mcp_configured", lambda: False)
    assert await bridge.get_bridge() is None


async def test_calls_to_one_server_are_serialized():
    """A single ClientSession isn't concurrency-safe; the bridge's per-server lock
    must serialize parallel tool calls so their streams don't interleave."""
    import asyncio

    order: list[str] = []

    class SlowSession:
        async def call_tool(self, name, args=None):
            order.append(f"enter:{name}")
            await asyncio.sleep(0.02)
            order.append(f"exit:{name}")
            return FakeResult(content=[FakeContent("ok")])

    b = MCPBridge()
    session = SlowSession()
    empty = {"type": "object", "properties": {}}
    b._register("srv", session, FakeTool("a", input_schema=empty))
    b._register("srv", session, FakeTool("b", input_schema=empty))
    try:
        await asyncio.gather(
            registry.call("mcp__srv__a"),
            registry.call("mcp__srv__b"),
        )
        # Each call fully enters and exits before the other enters — no interleave.
        assert order in (
            ["enter:a", "exit:a", "enter:b", "exit:b"],
            ["enter:b", "exit:b", "enter:a", "exit:a"],
        ), order
    finally:
        await b.aclose()
