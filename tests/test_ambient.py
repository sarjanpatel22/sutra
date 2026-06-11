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


def test_daemon_reconnects_after_memory_recovers(monkeypatch):
    """If FalkorDB is down then comes back, the daemon must resume persisting —
    initialize() caches availability, so flush() resets the store on total failure."""
    import asyncio

    import friday.ambient.daemon as dmod

    class FakeStore:
        def __init__(self):
            self.up = False
            self.reset_called = 0
            self.wrote = 0

        async def initialize(self):
            return self.up

        async def remember(self, text, source=None):
            if not self.up:
                return False
            self.wrote += 1
            return True

        def reset(self):
            self.reset_called += 1
            self.up = True  # simulate FalkorDB coming back

    fake = FakeStore()
    monkeypatch.setattr(dmod, "get_store", lambda: fake)

    class _Col:
        name = "test"
        def poll(self, *, idle): ...
        def drain(self): return ["Sarjan did a thing."]

    daemon = dmod.AmbientDaemon()
    daemon.collectors = [_Col()]

    n1 = asyncio.run(daemon.flush())   # DB down -> 0 written -> reset()
    n2 = asyncio.run(daemon.flush())   # reconnected -> writes
    assert n1 == 0 and fake.reset_called == 1
    assert n2 == 1  # recovered


def test_flush_skips_low_value_and_does_not_false_reset(monkeypatch):
    """Ambient telemetry ('spent 8m in Terminal') is dropped at the source; a real
    summary still persists, and the partial write is not mistaken for a DB failure."""
    import asyncio

    import friday.ambient.daemon as dmod

    class FakeStore:
        def __init__(self):
            self.remembered: list[str] = []
            self.reset_called = 0

        async def initialize(self):
            return True

        async def remember(self, text, source=None):
            self.remembered.append(text)
            return True

        def reset(self):
            self.reset_called += 1

    fake = FakeStore()
    monkeypatch.setattr(dmod, "get_store", lambda: fake)

    class _Col:
        name = "test"
        def poll(self, *, idle): ...
        def drain(self):
            return [
                "Sarjan spent about 8m in Terminal (mostly on Resume previous coding session).",
                "Sarjan edited store.py in the friday-os project.",
            ]

    daemon = dmod.AmbientDaemon()
    daemon.collectors = [_Col()]
    wrote = asyncio.run(daemon.flush())
    assert wrote == 1
    assert fake.remembered == ["Sarjan edited store.py in the friday-os project."]
    assert fake.reset_called == 0  # a real write succeeded — not a failure


def test_flush_all_low_value_skips_without_reset(monkeypatch):
    """A flush of pure telemetry writes nothing but must NOT reset the connection."""
    import asyncio

    import friday.ambient.daemon as dmod

    class FakeStore:
        def __init__(self):
            self.reset_called = 0

        async def initialize(self):
            return True

        async def remember(self, text, source=None):
            return True

        def reset(self):
            self.reset_called += 1

    fake = FakeStore()
    monkeypatch.setattr(dmod, "get_store", lambda: fake)

    class _Col:
        name = "test"
        def poll(self, *, idle): ...
        def drain(self):
            return ["Sarjan spent about 3m in firefox-esr (mostly on YouTube)."]

    daemon = dmod.AmbientDaemon()
    daemon.collectors = [_Col()]
    wrote = asyncio.run(daemon.flush())
    assert wrote == 0
    assert fake.reset_called == 0  # all-skipped is not a DB failure
