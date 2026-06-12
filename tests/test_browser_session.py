"""Tests for Sutra's persistent browser session — headless policy, guards, gating.

The live launch is exercised by a manual smoke (it needs a display + Chromium); here
we lock the offline-checkable parts: the headed/headless decision, the URL guard, the
auto-confirm posture, and that browser_open drives get_page.
"""

import friday.skills.browser_session as bs
from friday.skills import registry
from friday.skills.browser_session import _headless, browser_open


def test_headless_env_overrides(monkeypatch):
    monkeypatch.setenv("SUTRA_BROWSER_HEADLESS", "1")
    assert _headless() is True
    monkeypatch.setenv("SUTRA_BROWSER_HEADLESS", "0")
    assert _headless() is False


def test_headless_follows_display_when_unset(monkeypatch):
    monkeypatch.delenv("SUTRA_BROWSER_HEADLESS", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    assert _headless() is False  # a display exists → show the window
    monkeypatch.delenv("DISPLAY", raising=False)
    assert _headless() is True  # headless box → fall back, don't crash


def test_browser_open_is_auto_and_audit_clean():
    registry.autodiscover()
    assert registry.get("browser_open").auto_confirm is True  # navigation is reversible
    from friday.skills import audit

    metas = [registry.get(n) for n in registry.list_skills()]
    assert not audit.all_problems(metas)  # browser_open is a reviewed auto exception


async def test_browser_open_rejects_non_http():
    out = await browser_open("file:///etc/passwd")
    assert "http" in out["error"].lower()


async def test_browser_open_drives_get_page(monkeypatch):
    class FakePage:
        url = "https://example.com/"

        async def goto(self, url, **k):
            type(self).url = url

        async def title(self):
            return "Example"

    async def fake_get_page():
        return FakePage()

    monkeypatch.setattr(bs, "get_page", fake_get_page)
    out = await browser_open("https://example.com")
    assert out["status"] == "ok" and out["title"] == "Example"
    assert "sign in" in out["note"].lower()
