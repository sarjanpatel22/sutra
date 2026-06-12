"""Skill registry unit tests."""

import pytest

from friday.skills.registry import _registry, all_tools, call, skill


@pytest.fixture(autouse=True)
def clear_registry():
    old = dict(_registry)
    _registry.clear()
    yield
    _registry.clear()
    _registry.update(old)


def test_skill_decorator_registers():
    @skill(name="test_echo", description="Echo a value")
    async def echo(message: str) -> str:
        return message

    assert "test_echo" in _registry
    assert _registry["test_echo"].auto_confirm is True


def test_schema_generation():
    @skill(name="test_add", description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    tools = all_tools()
    assert any(t["function"]["name"] == "test_add" for t in tools)
    schema = next(t for t in tools if t["function"]["name"] == "test_add")
    params = schema["function"]["parameters"]
    assert "a" in params["properties"]
    assert "b" in params["properties"]
    assert params["properties"]["a"]["type"] == "integer"
    assert "a" in params["required"]


@pytest.mark.asyncio
async def test_call_skill():
    @skill(name="test_double", description="Double a number")
    async def double(x: int) -> int:
        return x * 2

    result = await call("test_double", x=5)
    assert result == 10


def test_get_unknown_skill_raises():
    from friday.skills.registry import get
    with pytest.raises(KeyError):
        get("nonexistent_skill")
