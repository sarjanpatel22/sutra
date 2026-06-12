"""Tests for git skills — real git against a throwaway repo under $HOME.

_safe_path confines skills to the home directory, so the fixture builds its repo
in ~/.cache (pytest's tmp_path lives in /tmp and would be rejected — that
rejection is itself part of the contract and is tested below).
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from friday.skills import registry
from friday.skills.git_tools import git_commit, git_diff, git_status


@pytest.fixture()
def repo():
    base = Path.home() / ".cache" / "friday-tests"
    base.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=base))
    subprocess.run(["git", "-C", str(d), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)
    (d / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", "init"], check=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_gating():
    registry.autodiscover()
    assert registry.get("git_status").auto_confirm is True
    assert registry.get("git_diff").auto_confirm is True
    assert registry.get("git_commit").auto_confirm is False  # writes history → confirm


async def test_status_clean_and_dirty(repo):
    out = await git_status(str(repo))
    assert out["clean"] is True and out["branch"].startswith("main")
    (repo / "a.txt").write_text("two\n")
    out = await git_status(str(repo))
    assert out["clean"] is False and any("a.txt" in c for c in out["changes"])


async def test_diff_working_tree(repo):
    (repo / "a.txt").write_text("two\n")
    out = await git_diff(str(repo))
    assert "+two" in out["diff"] and out["empty"] is False


async def test_commit_stages_and_commits(repo):
    (repo / "a.txt").write_text("two\n")
    out = await git_commit("change a", repo=str(repo), paths=["a.txt"])
    assert "commit" in out and len(out["commit"]) >= 7
    assert (await git_status(str(repo)))["clean"] is True


async def test_commit_requires_paths_or_all(repo):
    assert "error" in await git_commit("msg", repo=str(repo))
    assert "error" in await git_commit("", repo=str(repo), all_changes=True)


async def test_rejects_non_repo_and_outside_home(repo):
    plain = repo / "sub"
    plain.mkdir()
    assert "not a git repository" in (await git_status(str(plain)))["error"]
    assert "error" in await git_status("/tmp")
