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


def test_autodiscover_skips_broken_skill_module(monkeypatch):
    """A skill that fails to import (e.g. missing optional dep) must be skipped and
    recorded — never crash discovery and take every other skill down with it."""
    import friday.skills.registry as reg

    real_import = reg.importlib.import_module
    imported: list[str] = []

    def fake_import(name, *a, **k):
        if name.endswith(".broken_skill_xyz"):
            raise ImportError("No module named 'totally_missing_dep'")
        imported.append(name)
        return real_import(name, *a, **k)

    # Broken module FIRST: if discovery weren't resilient it would crash here and never
    # reach the good sibling — so seeing system_info imported proves it continued.
    def fake_iter(_paths):
        yield (None, "broken_skill_xyz", False)   # missing-dep skill — must be skipped
        yield (None, "system_info", False)        # good module after it — must still load

    monkeypatch.setattr(reg.importlib, "import_module", fake_import)
    monkeypatch.setattr(reg.pkgutil, "iter_modules", fake_iter)

    reg.autodiscover()  # must NOT raise despite the broken module

    failed = dict(reg.failed_imports())
    assert "broken_skill_xyz" in failed
    assert "ImportError" in failed["broken_skill_xyz"]
    assert "system_info" not in failed                          # good sibling unaffected
    assert any(n.endswith(".system_info") for n in imported), \
        "discovery stopped at the broken module instead of continuing"
