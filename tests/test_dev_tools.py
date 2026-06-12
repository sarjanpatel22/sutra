"""Tests for run_tests — live pytest-in-pytest against a throwaway project."""

import shutil
import tempfile
from pathlib import Path

import pytest

from friday.skills import registry
from friday.skills.dev_tools import run_tests


@pytest.fixture()
def proj():
    base = Path.home() / ".cache" / "friday-tests"
    base.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=base))
    (d / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (d / "test_bad.py").write_text("def test_bad():\n    assert False\n")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_run_tests_is_gated():
    registry.autodiscover()
    assert registry.get("run_tests").auto_confirm is False  # executes repo code → confirm


async def test_passing_file(proj):
    out = await run_tests(path="test_ok.py", repo=str(proj))
    assert out["passed"] is True and out["exit_code"] == 0
    assert "1 passed" in out["summary"]


async def test_failing_file_reports_failure(proj):
    out = await run_tests(path="test_bad.py", repo=str(proj))
    assert out["passed"] is False and out["exit_code"] != 0
    assert "1 failed" in out["summary"]


async def test_k_expression_selects(proj):
    out = await run_tests(path=".", expr="ok", repo=str(proj))
    assert out["passed"] is True and "1 passed" in out["summary"]


async def test_bad_repo_rejected(proj):
    assert "error" in await run_tests(repo="/etc")
    assert "error" in await run_tests(repo=str(proj / "missing"))
