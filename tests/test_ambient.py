"""Ambient collector logic tests (pure parts — no live polling)."""

from __future__ import annotations

from pathlib import Path

from friday.ambient import config
from friday.ambient.collectors import (
    FileActivityCollector,
    GitActivityCollector,
    ShellHistoryCollector,
    WindowFocusCollector,
    _and_list,
    fmt_dur,
)


def test_fmt_dur():
    assert fmt_dur(45) == "45s"
    assert fmt_dur(60) == "1m"
    assert fmt_dur(3600) == "1h"
    assert fmt_dur(7800) == "2h10m"


def test_and_list():
    assert _and_list([]) == ""
    assert _and_list(["a"]) == "a"
    assert _and_list(["a", "b"]) == "a and b"
    assert _and_list(["a", "b", "c"]) == "a, b and c"


def test_shell_base_command():
    bc = ShellHistoryCollector.base_command
    assert bc(": 1700000000:0;git push origin main") == "git"
    assert bc("sudo docker start sutra-falkordb") == "docker"
    assert bc("FOO=bar python script.py") == "python"
    assert bc("/usr/bin/ollama list") == "ollama"
    assert bc(":;ls") == "ls"
    assert bc("   ") == ""


def test_sensitive_paths_are_blocked():
    assert config.is_sensitive(Path.home() / ".ssh" / "id_rsa")
    assert config.is_sensitive(Path("/home/sarjan/proj/.env"))
    assert config.is_sensitive(Path("/x/my_secret.txt"))
    assert not config.is_sensitive(Path("/home/sarjan/friday-os/main.py"))


def test_noise_is_ignored():
    assert config.is_noise(Path("x/__pycache__/m.pyc"))
    assert config.is_noise(Path("a.pyc"))
    assert config.is_noise(Path("proj/build/out.txt"))
    assert not config.is_noise(Path("proj/main.py"))


def test_window_drain_summarizes_durations():
    c = WindowFocusCollector()
    c._dur["VS Code"] = 7800
    c._dur["Chromium"] = 40  # below 60s threshold -> dropped
    out = c.drain()
    assert any("VS Code" in s and "2h10m" in s for s in out)
    assert not any("Chromium" in s for s in out)
    assert c.drain() == []  # state reset


def test_file_activity_respects_filters(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("x")
    (tmp_path / ".env").write_text("SECRET=1")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "m.pyc").write_text("x")
    monkeypatch.setattr(config, "WATCH_DIRS", [tmp_path])

    c = FileActivityCollector()
    c._since = 0  # everything counts as recent
    out = " ".join(c.drain())
    assert "main.py" in out
    assert ".env" not in out and "m.pyc" not in out


def test_git_first_run_does_not_replay(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()  # looks like a repo but has no HEAD
    monkeypatch.setattr(config, "WATCH_DIRS", [tmp_path])
    c = GitActivityCollector()
    assert c.drain() == []  # no commits surfaced on first observation


def test_shell_reads_only_new_lines(tmp_path):
    hist = tmp_path / ".zsh_history"
    hist.write_text(": 1:0;old command\n")
    c = ShellHistoryCollector(hist=hist)  # offset starts at end
    with hist.open("a") as f:
        f.write(": 2:0;git status\n: 3:0;docker ps\n: 4:0;git log\n")
    out = c.drain()
    assert out and "git" in out[0]
    assert "old" not in " ".join(out)
    assert c.drain() == []  # nothing new the second time
