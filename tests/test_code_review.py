"""Tests for code_review — diff plumbing and prompt assembly, no live LLM."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

import friday.skills.code_review as cr
from friday.skills import registry
from friday.skills.code_review import code_review


@pytest.fixture()
def repo():
    base = Path.home() / ".cache" / "friday-tests"
    base.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=base))
    subprocess.run(["git", "-C", str(d), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)
    (d / "a.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", "init"], check=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


class _Fake:
    """Shape-compatible stand-in for a router completion response."""

    def __init__(self, text):
        msg = type("M", (), {"content": text})
        self.choices = [type("C", (), {"message": msg})]


def test_code_review_is_auto():
    registry.autodiscover()
    assert registry.get("code_review").auto_confirm is True  # read-only


async def test_empty_diff_skips_llm(repo, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("LLM must not be called for an empty diff")

    monkeypatch.setattr(cr, "complete", boom)
    out = await code_review(str(repo))
    assert "nothing to review" in out["review"] and out["diff_chars"] == 0


async def test_diff_reaches_model_and_review_returns(repo, monkeypatch):
    (repo / "a.py").write_text("def f():\n    return 2\n")
    seen = {}

    async def fake(messages, **k):
        seen["user"] = messages[1]["content"]
        return _Fake("1. [bug] a.py: f now returns 2")

    monkeypatch.setattr(cr, "complete", fake)
    out = await code_review(str(repo), focus="correctness")
    assert "return 2" in seen["user"] and "focusing on correctness" in seen["user"]
    assert out["review"].startswith("1. [bug]") and out["truncated"] is False


async def test_huge_diff_truncated(repo, monkeypatch):
    (repo / "a.py").write_text("\n".join(f"x{i} = {i}" for i in range(2000)) + "\n")

    async def fake(messages, **k):
        assert "[diff truncated" in messages[1]["content"]
        return _Fake("LGTM — no findings")

    monkeypatch.setattr(cr, "complete", fake)
    out = await code_review(str(repo))
    assert out["truncated"] is True and out["diff_chars"] > cr._MAX_DIFF


async def test_bad_repo(repo):
    assert "error" in await code_review(str(repo / "nope"))
