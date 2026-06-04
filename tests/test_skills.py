"""Tests for the foundation skills' pure logic.

Covers what's deterministic and offline: calc evaluation + sandboxing, the
current_time skill, open_url's scheme validation, and that the full Phase-1.1
skill set (15+) is registered. Network/desktop/hardware skills (weather,
clipboard, battery) are exercised live, not here.
"""

import pytest

from friday.skills.calc import calc, evaluate
from friday.skills.clock import current_time
from friday.skills.desktop import _is_safe_url

# --- calc ---------------------------------------------------------------------

def test_calc_arithmetic():
    assert evaluate("2*(3+4)") == 14
    assert evaluate("2**10") == 1024
    assert evaluate("7 % 3") == 1
    assert evaluate("17 // 5") == 3


def test_calc_functions_and_constants():
    assert round(evaluate("sqrt(16)")) == 4
    assert round(evaluate("log(100, 10)")) == 2
    assert evaluate("factorial(5)") == 120
    assert round(evaluate("sin(pi/2)")) == 1


def test_calc_blocks_code_execution():
    for hostile in (
        "__import__('os').system('echo hi')",
        "(1).__class__",
        "open('/etc/passwd')",
    ):
        with pytest.raises(Exception):
            evaluate(hostile)


def test_calc_guards_runaway_exponent():
    with pytest.raises(ValueError):
        evaluate("9 ** 99999")


async def test_calc_skill_returns_error_not_raise():
    out = await calc("2 +")  # syntax error
    assert "error" in out


# --- current_time -------------------------------------------------------------

async def test_current_time_shape():
    out = await current_time("UTC")
    assert {"iso", "date", "time", "weekday", "timezone"}.issubset(out)
    assert out["timezone"] == "UTC"


async def test_current_time_bad_timezone():
    out = await current_time("Not/AZone")
    assert "error" in out


# --- open_url scheme validation ----------------------------------------------

def test_open_url_accepts_http_https():
    assert _is_safe_url("https://example.com")
    assert _is_safe_url("http://localhost:8700/path?q=1")


def test_open_url_rejects_dangerous_schemes():
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x", "not a url", ""):
        assert not _is_safe_url(bad)


# --- registry completeness ----------------------------------------------------

def test_foundation_skills_registered():
    from friday.skills.registry import autodiscover, list_skills

    autodiscover()
    skills = set(list_skills())
    expected = {
        "calc", "current_time", "weather", "clipboard_get", "clipboard_set",
        "open_url", "battery", "web_search", "read_file", "write_file",
        "list_directory", "system_info", "run_command", "recall", "forget",
    }
    missing = expected - skills
    assert not missing, f"missing skills: {missing}"
    assert len(skills) >= 15
