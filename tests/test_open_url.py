"""open_url reuses the already-open browser window (new tab) instead of spawning a new
window each call — and a rapid burst cold-starts the browser exactly once."""

import asyncio

import friday.skills.desktop as d


def test_reuse_cmd_firefox_forces_tab_chromium_is_default():
    # Firefox needs an explicit --new-tab or a pref can send it to a new window; chromium
    # already opens a tab in the running window.
    assert d._reuse_cmd("/usr/bin/firefox-esr", "u") == ["/usr/bin/firefox-esr", "--new-tab", "u"]
    assert d._reuse_cmd("/usr/bin/chromium", "u") == ["/usr/bin/chromium", "u"]


def test_default_browser_resolves_x_www_browser(monkeypatch):
    monkeypatch.setattr(d.shutil, "which",
                        lambda n: "/usr/bin/x-www-browser" if n == "x-www-browser" else None)
    monkeypatch.setattr(d.os.path, "realpath", lambda p: "/usr/bin/firefox-esr")
    assert d._default_browser() == "/usr/bin/firefox-esr"


async def test_rejects_non_http_url():
    out = await d.open_url("file:///etc/passwd")
    assert "error" in out


async def test_reuses_running_browser_as_a_tab(monkeypatch):
    calls = []

    async def fake_launch(cmd, settle_s=4.0):
        calls.append(cmd)
        return 0, ""

    async def running(_b):
        return True

    monkeypatch.setattr(d, "_default_browser", lambda: "/usr/bin/firefox-esr")
    monkeypatch.setattr(d, "_browser_running", running)
    monkeypatch.setattr(d, "_launch_detached", fake_launch)

    out = await d.open_url("https://youtube.com")
    assert out["status"] == "ok" and out["reused"] is True
    assert calls == [["/usr/bin/firefox-esr", "--new-tab", "https://youtube.com"]]


async def test_cold_start_launches_once_then_waits(monkeypatch):
    calls, waited = [], []

    async def fake_launch(cmd, settle_s=4.0):
        calls.append(cmd)
        return None, "still launching"  # handed off, browser warming

    async def not_running(_b):
        return False

    async def fake_wait(b, timeout=6.0):
        waited.append(b)

    monkeypatch.setattr(d, "_default_browser", lambda: "/usr/bin/firefox-esr")
    monkeypatch.setattr(d, "_browser_running", not_running)
    monkeypatch.setattr(d, "_launch_detached", fake_launch)
    monkeypatch.setattr(d, "_wait_until_running", fake_wait)

    out = await d.open_url("https://youtube.com")
    assert out["status"] == "ok" and out["reused"] is False
    assert calls == [["/usr/bin/firefox-esr", "https://youtube.com"]]  # no --new-tab cold
    assert waited == ["/usr/bin/firefox-esr"]  # waited so the next call reuses this window


async def test_falls_back_to_xdg_open_without_a_browser(monkeypatch):
    calls = []

    async def fake_launch(cmd, settle_s=4.0):
        calls.append(cmd)
        return 0, ""

    monkeypatch.setattr(d, "_default_browser", lambda: None)
    monkeypatch.setattr(d, "_launch_detached", fake_launch)
    monkeypatch.setattr(d.shutil, "which",
                        lambda n: "/usr/bin/xdg-open" if n == "xdg-open" else None)

    out = await d.open_url("https://example.com")
    assert out["status"] == "ok"
    assert calls == [["xdg-open", "https://example.com"]]


async def test_burst_cold_starts_exactly_once(monkeypatch):
    """THE multi-window fix: three near-simultaneous calls must open ONE window then add
    tabs — not race into three windows (the live football-audio false-trigger storm)."""
    launches = []
    state = {"running": False}

    async def fake_launch(cmd, settle_s=4.0):
        launches.append(cmd)
        return None, ""

    async def running(_b):
        return state["running"]

    async def fake_wait(_b, timeout=6.0):
        state["running"] = True  # the cold-started browser is now up

    monkeypatch.setattr(d, "_default_browser", lambda: "/usr/bin/firefox-esr")
    monkeypatch.setattr(d, "_browser_running", running)
    monkeypatch.setattr(d, "_launch_detached", fake_launch)
    monkeypatch.setattr(d, "_wait_until_running", fake_wait)

    await asyncio.gather(*(d.open_url("https://x.com") for _ in range(3)))

    cold = [c for c in launches if "--new-tab" not in c]
    tabs = [c for c in launches if "--new-tab" in c]
    assert len(cold) == 1   # exactly one window opened
    assert len(tabs) == 2   # the rest landed as tabs in it
