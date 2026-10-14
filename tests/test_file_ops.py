"""Tests for the file_ops skill — read/write/edit/list, with the home-dir safety guard."""

from __future__ import annotations

import pytest

from friday.skills import file_ops


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the skill's home sandbox at a temp dir so writes are isolated and allowed."""
    monkeypatch.setattr(file_ops, "_HOME", tmp_path)
    return tmp_path


async def test_read_file_roundtrip(home):
    f = home / "note.txt"
    f.write_text("hello world")
    out = await file_ops.read_file(str(f))
    assert out["content"] == "hello world" and out["bytes_read"] == 11


async def test_read_file_missing(home):
    out = await file_ops.read_file(str(home / "nope.txt"))
    assert "error" in out and "not found" in out["error"].lower()


async def test_read_file_truncates(home):
    f = home / "big.txt"
    f.write_text("x" * 100)
    out = await file_ops.read_file(str(f), max_bytes=10)
    assert out["bytes_read"] == 10 and out["truncated"] == "True"


async def test_write_file_creates_parents(home):
    f = home / "sub" / "deep" / "a.txt"
    out = await file_ops.write_file(str(f), "data")
    assert out["status"] == "ok" and f.read_text() == "data"


async def test_edit_file_unique_match(home):
    f = home / "cfg.txt"
    f.write_text("port = 8080\nhost = x")
    out = await file_ops.edit_file(str(f), "8080", "9090")
    assert out["status"] == "ok" and "9090" in f.read_text()


async def test_edit_file_ambiguous_without_replace_all(home):
    f = home / "d.txt"
    f.write_text("a a a")
    out = await file_ops.edit_file(str(f), "a", "b")
    assert "error" in out and "matches 3" in out["error"]
    assert f.read_text() == "a a a"  # unchanged on error


async def test_edit_file_replace_all(home):
    f = home / "d2.txt"
    f.write_text("a a a")
    out = await file_ops.edit_file(str(f), "a", "b", replace_all=True)
    assert out["replacements"] == 3 and f.read_text() == "b b b"


async def test_edit_file_not_found_is_error(home):
    out = await file_ops.edit_file(str(home / "ghost.txt"), "x", "y")
    assert "error" in out


async def test_list_directory_filters_hidden(home):
    (home / "visible.txt").write_text("")
    (home / ".hidden").write_text("")
    out = await file_ops.list_directory(str(home))
    assert "visible.txt" in out["entries"] and ".hidden" not in out["entries"]


async def test_list_directory_not_a_dir_error_is_string(home):
    f = home / "file.txt"
    f.write_text("")
    out = await file_ops.list_directory(str(f))
    # error is a plain string, consistent with every other skill (not a list)
    assert isinstance(out["error"], str) and "Not a directory" in out["error"]


async def test_safe_path_blocks_outside_home(home):
    # A path outside the home sandbox is rejected (the containment guard).
    with pytest.raises(PermissionError):
        file_ops._safe_path("/etc/passwd")
