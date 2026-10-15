"""Tests for the desktop skill helpers (friday/skills/desktop.py).

No real display/clipboard/browser is touched: _run and the tool/browser
resolvers are monkeypatched. Covers the URL safety gate, the firefox tab-reuse
invocation, and the clipboard read/write happy + degraded paths.
"""

from __future__ import annotations

import pytest

from friday.skills import desktop

# --- _is_safe_url: only http(s) with a host ------------------------------

@pytest.mark.parametrize("url", [
    "http://example.com",
    "https://example.com/path?q=1",
    "https://sub.domain.co.uk",
])
def test_safe_urls_allowed(url: str) -> None:
    assert desktop._is_safe_url(url) is True


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "javascript:alert(1)",
    "ftp://host/x",
    "http://",            # no netloc
    "not a url",
    "",
])
def test_unsafe_urls_rejected(url: str) -> None:
    assert desktop._is_safe_url(url) is False


# --- _reuse_cmd: firefox needs --new-tab, chromium family doesn't ---------

def test_reuse_cmd_firefox_forces_tab() -> None:
    assert desktop._reuse_cmd("/usr/bin/firefox", "https://x.com") == \
        ["/usr/bin/firefox", "--new-tab", "https://x.com"]


def test_reuse_cmd_chromium_plain() -> None:
    assert desktop._reuse_cmd("/usr/bin/chromium", "https://x.com") == \
        ["/usr/bin/chromium", "https://x.com"]


# --- clipboard_get -------------------------------------------------------

async def test_clipboard_get_success(monkeypatch) -> None:
    monkeypatch.setattr(desktop, "_clipboard_get_cmd", lambda: ["xclip", "-o"])

    async def fake_run(cmd, stdin=None):
        return 0, "copied text", ""
    monkeypatch.setattr(desktop, "_run", fake_run)

    assert await desktop.clipboard_get() == {"text": "copied text"}


async def test_clipboard_get_no_tool(monkeypatch) -> None:
    monkeypatch.setattr(desktop, "_clipboard_get_cmd", lambda: None)
    res = await desktop.clipboard_get()
    assert "error" in res and "clipboard tool" in res["error"]


async def test_clipboard_get_failure(monkeypatch) -> None:
    monkeypatch.setattr(desktop, "_clipboard_get_cmd", lambda: ["xclip", "-o"])

    async def fake_run(cmd, stdin=None):
        return 1, "", "boom"
    monkeypatch.setattr(desktop, "_run", fake_run)

    res = await desktop.clipboard_get()
    assert res == {"error": "boom"}


# --- clipboard_set -------------------------------------------------------

async def test_clipboard_set_pipes_stdin(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def fake_run(cmd, stdin=None):
        seen["cmd"], seen["stdin"] = cmd, stdin
        return 0, "", ""
    monkeypatch.setattr(desktop, "_clipboard_set_cmd", lambda: ["xclip", "-sel", "c"])
    monkeypatch.setattr(desktop, "_run", fake_run)

    res = await desktop.clipboard_set("hÉllo")
    assert res == {"status": "ok", "bytes": len("hÉllo".encode())}
    assert seen["stdin"] == "hÉllo"          # text is piped via stdin, not argv
    assert seen["cmd"] == ["xclip", "-sel", "c"]


async def test_clipboard_set_no_tool(monkeypatch) -> None:
    monkeypatch.setattr(desktop, "_clipboard_set_cmd", lambda: None)
    res = await desktop.clipboard_set("x")
    assert "error" in res


# --- open_url: the safety gate fires before any launch -------------------

async def test_open_url_rejects_non_http() -> None:
    res = await desktop.open_url("file:///etc/passwd")
    assert res == {"error": "only http(s) URLs are allowed", "url": "file:///etc/passwd"}
