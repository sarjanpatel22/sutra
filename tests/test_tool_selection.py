"""On-demand tool selection (registry.select_tools) — expose the core + turn-relevant skills,
not all 50+, so the local function-caller stays focused and the prompt stays cheap."""

from __future__ import annotations

from friday.skills import registry


def _names(tools: list[dict]) -> set[str]:
    return {t["function"]["name"] for t in tools}


def test_no_cap_or_fits_returns_all():
    registry.autodiscover()
    total = len(registry.all_tools())
    assert len(registry.select_tools("anything", limit=0)) == total          # 0 = show everything
    assert len(registry.select_tools("anything", limit=total + 5)) == total   # already fits


def test_core_always_present_and_irrelevant_dropped():
    registry.autodiscover()
    out = registry.select_tools("xyzzy plugh foobar", limit=20)  # matches no specialized skill
    names = _names(out)
    for core in ("web_search", "write_file", "read_file", "run_command", "run_python", "calc"):
        assert core in names
    assert "weather" not in names          # specialized + irrelevant → pruned
    assert "transform_text" not in names
    assert len(out) <= 20


def test_relevant_specialized_skill_surfaces():
    registry.autodiscover()
    enc = _names(registry.select_tools("decode this base64 string", limit=20))
    assert "transform_text" in enc

    wx = _names(registry.select_tools("what is the weather in Toronto today", limit=20))
    assert "weather" in wx

    git = _names(registry.select_tools("commit my staged changes with a message", limit=20))
    assert "git_commit" in git


def test_cap_is_respected_with_core_floor():
    registry.autodiscover()
    out = registry.select_tools("search the web and write a file and check the weather", limit=18)
    # never more than the cap (core floor is below 18), and core is still all there
    assert len(out) <= 18
    assert "web_search" in _names(out)
