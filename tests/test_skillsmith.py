"""Tests for self-extending skills — drafting stays INERT; only explicit approval promotes."""

from friday.core import skillsmith
from friday.skills import registry


class _Msg:
    def __init__(self, content):
        self.content = content


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": _Msg(content)})()]


_SAFE_SKILL = (
    "from friday.skills.registry import skill\n\n"
    '@skill(name="reverse_text", description="Reverse a string.", auto_confirm=True, timeout_s=5)\n'
    "async def reverse_text(text: str = \"\") -> dict:\n"
    '    """Reverse text."""\n'
    "    return {\"reversed\": text[::-1]}\n"
)


def test_scan_flags_dangerous_and_passes_safe():
    assert any("subprocess" in f for f in skillsmith.scan_code("import subprocess as s"))
    assert skillsmith.scan_code("eval('1+1')")
    assert any("deletes files" in f for f in skillsmith.scan_code("os.remove('/x')"))
    assert skillsmith.scan_code(_SAFE_SKILL) == []  # the safe template trips nothing


def test_extract_name_decorator_then_def_then_fallback():
    assert skillsmith._extract_name(_SAFE_SKILL, "fb") == "reverse_text"
    assert skillsmith._extract_name("async def foo():\n    pass", "fb") == "foo"
    assert skillsmith._extract_name("x = 1", "fb") == "fb"


async def test_draft_writes_inert_file_and_does_not_register(monkeypatch, tmp_path):
    monkeypatch.setattr(skillsmith, "_PENDING_DIR", tmp_path / "pending")

    async def _fake_complete(messages, tier=None, tools=None, **kw):
        return _Resp("```python\n" + _SAFE_SKILL + "```")

    monkeypatch.setattr(skillsmith, "complete", _fake_complete)
    out = await skillsmith.draft_skill("reverse a string")
    assert out["name"] == "reverse_text"
    assert (tmp_path / "pending" / "reverse_text.py").is_file()  # inert source written
    assert out["flags"] == []
    # CRITICAL: drafting must never execute or register the skill — only approval can.
    assert "reverse_text" not in registry.list_skills()


async def test_draft_rejects_unparseable_code(monkeypatch, tmp_path):
    monkeypatch.setattr(skillsmith, "_PENDING_DIR", tmp_path / "pending")

    async def _bad(messages, tier=None, tools=None, **kw):
        return _Resp("def broken(:\n  not python")

    monkeypatch.setattr(skillsmith, "complete", _bad)
    out = await skillsmith.draft_skill("x")
    assert "error" in out and "parse" in out["error"]
    assert not (tmp_path / "pending").exists()  # nothing staged on a bad draft


def test_approve_moves_pending_then_reject(monkeypatch, tmp_path):
    monkeypatch.setattr(skillsmith, "_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(skillsmith, "USER_SKILLS_DIR", tmp_path / "user")
    (tmp_path / "pending").mkdir()
    (tmp_path / "pending" / "foo.py").write_text(_SAFE_SKILL)

    dst = skillsmith.approve_skill("foo")
    assert dst is not None and dst.is_file()
    assert not (tmp_path / "pending" / "foo.py").exists()  # moved, not left behind
    assert skillsmith.approve_skill("missing") is None

    (tmp_path / "pending" / "bar.py").write_text("x")
    assert skillsmith.reject_skill("bar") is True
    assert skillsmith.reject_skill("bar") is False


def test_registry_loads_an_approved_user_skill(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "USER_SKILLS_DIR", tmp_path)
    monkeypatch.setattr(registry, "_loaded_user", set())
    code = (
        "from friday.skills.registry import skill\n\n"
        '@skill(name="user_demo_skill", description="demo", auto_confirm=True, timeout_s=5)\n'
        "async def user_demo_skill() -> dict:\n"
        "    return {\"ok\": True}\n"
    )
    (tmp_path / "user_demo_skill.py").write_text(code)
    try:
        registry.autodiscover()
        assert "user_demo_skill" in registry.list_skills()  # approved skill loaded for real
    finally:
        registry._registry.pop("user_demo_skill", None)
