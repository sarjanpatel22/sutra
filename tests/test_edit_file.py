"""Tests for edit_file — targeted in-place edits with unique-match safety."""

import shutil
import tempfile
from pathlib import Path

import pytest

from friday.skills import registry
from friday.skills.file_ops import edit_file


@pytest.fixture()
def tmp_file():
    base = Path.home() / ".cache" / "friday-tests"
    base.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=base))
    f = d / "code.py"
    f.write_text("x = 1\ny = 2\nx = 1\n")
    yield f
    shutil.rmtree(d, ignore_errors=True)


def test_edit_file_is_gated():
    registry.autodiscover()
    assert registry.get("edit_file").auto_confirm is False  # in-place mutation → confirm


async def test_unique_replacement(tmp_file):
    out = await edit_file(str(tmp_file), "y = 2", "y = 99")
    assert out["status"] == "ok" and out["replacements"] == 1
    assert tmp_file.read_text() == "x = 1\ny = 99\nx = 1\n"


async def test_ambiguous_match_refused(tmp_file):
    out = await edit_file(str(tmp_file), "x = 1", "x = 0")
    assert "matches 2 times" in out["error"]
    assert tmp_file.read_text() == "x = 1\ny = 2\nx = 1\n"  # untouched


async def test_replace_all(tmp_file):
    out = await edit_file(str(tmp_file), "x = 1", "x = 0", replace_all=True)
    assert out["replacements"] == 2
    assert tmp_file.read_text() == "x = 0\ny = 2\nx = 0\n"


async def test_missing_text_and_file(tmp_file):
    assert "not found in file" in (await edit_file(str(tmp_file), "nope", "x"))["error"]
    assert "File not found" in (await edit_file(str(tmp_file.parent / "z.py"), "a", "b"))["error"]


async def test_degenerate_inputs(tmp_file):
    assert "empty" in (await edit_file(str(tmp_file), "", "x"))["error"]
    assert "identical" in (await edit_file(str(tmp_file), "x = 1", "x = 1"))["error"]
