"""Desktop interaction skills — clipboard and opening URLs (X11 + Wayland)."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from friday.skills.registry import skill

# Serialize browser launches: a rapid burst of open_url calls (e.g. repeated voice
# triggers) must NOT each cold-start the browser into its own window. The first call
# launches it; the rest wait, see it running, and reuse it as a new tab.
_BROWSER_LAUNCH_LOCK = asyncio.Lock()

# Browsers we know how to drive directly (resolve the default, detect if it's running,
# and force a new TAB rather than a new window).
_BROWSER_TOKENS = ("firefox", "chromium", "chrome", "brave")
_KNOWN_BROWSERS = (
    "firefox-esr", "firefox", "chromium", "chromium-browser",
    "google-chrome", "google-chrome-stable", "brave-browser", "brave",
)


async def _run(cmd: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin.encode() if stdin is not None else None)
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def _launch_detached(cmd: list[str], settle_s: float = 4.0) -> tuple[int | None, str]:
    """Fire-and-forget a GUI launcher (e.g. xdg-open) and return promptly.

    The bug this avoids: piping stdout/stderr and calling communicate() blocks
    until those pipes hit EOF — but the browser xdg-open spawns INHERITS the pipe
    and holds it open for its whole lifetime, so we'd wait for the browser to
    CLOSE (→ the 20s skill-timeout kill). So: detach into its own session, send
    stdio to /dev/null (no pipe to block on), and wait only for the launcher's
    own exit. If it hasn't returned by settle_s a cold browser is still coming
    up — it has already handed off, so we report success and leave it running
    rather than killing the browser mid-start.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        code = await asyncio.wait_for(proc.wait(), timeout=settle_s)
        return code, ""
    except TimeoutError:
        return None, "still launching"  # handed off; browser is warming up


def _clipboard_get_cmd() -> list[str] | None:
    if shutil.which("wl-paste"):
        return ["wl-paste", "--no-newline"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-o"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--output"]
    return None


def _clipboard_set_cmd() -> list[str] | None:
    if shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    return None


@skill(
    name="clipboard_get",
    description="Read what's currently on the system clipboard — 'what's in my clipboard', "
    "'what did I copy'.",
    auto_confirm=True,
    timeout_s=5,
)
async def clipboard_get() -> dict[str, Any]:
    """Return the clipboard's current text."""
    cmd = _clipboard_get_cmd()
    if not cmd:
        return {"error": "no clipboard tool found (install wl-clipboard, xclip, or xsel)"}
    code, out, err = await _run(cmd)
    if code != 0:
        return {"error": err.strip() or "clipboard read failed"}
    return {"text": out}


@skill(
    name="clipboard_set",
    description="Put text onto the system clipboard — 'copy X to my clipboard'. To TYPE into "
    "the focused window instead, use type_text.",
    auto_confirm=True,
    timeout_s=5,
)
async def clipboard_set(text: str) -> dict[str, Any]:
    """Put text on the clipboard.

    text: The text to copy.
    """
    cmd = _clipboard_set_cmd()
    if not cmd:
        return {"error": "no clipboard tool found (install wl-clipboard, xclip, or xsel)"}
    code, _, err = await _run(cmd, stdin=text)
    if code != 0:
        return {"error": err.strip() or "clipboard write failed"}
    return {"status": "ok", "bytes": len(text.encode())}


def _is_safe_url(url: str) -> bool:
    """Allow only http(s) URLs — never file:, javascript:, etc., via xdg-open."""
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _default_browser() -> str | None:
    """Absolute path to the default browser binary, or None if not resolvable.

    Resolves the Debian `x-www-browser` alternative (Kali's `default-browser` wrapper,
    which `xdg-open` eventually hits, ends here), falling back to the first known browser
    on PATH. We drive this binary directly so we can force a tab and detect reuse —
    `xdg-open` goes through a wrapper that just relaunches and can't be steered.
    """
    xb = shutil.which("x-www-browser")
    if xb:
        real = os.path.realpath(xb)
        if any(tok in Path(real).name for tok in _BROWSER_TOKENS):
            return real
    for b in _KNOWN_BROWSERS:
        if (p := shutil.which(b)):
            return p
    return None


def _reuse_cmd(browser: str, url: str) -> list[str]:
    """Invocation that opens `url` as a new TAB in the already-running browser.

    Firefox needs an explicit `--new-tab` or a `browser.link.open_newwindow` pref can
    send it to a new window; chromium/chrome/brave already reuse the window by default.
    """
    if "firefox" in Path(browser).name:
        return [browser, "--new-tab", url]
    return [browser, url]


async def _browser_running(browser: str) -> bool:
    """True if a process for this browser is already up (so we open a tab, not a window)."""
    if not shutil.which("pgrep"):
        return False
    code, _, _ = await _run(["pgrep", "-f", Path(browser).name])
    return code == 0


async def _wait_until_running(browser: str, timeout: float = 6.0) -> None:
    """After a cold launch, wait until the browser is up so the NEXT open_url lands as a
    tab in this window instead of racing into a second one. Held under the launch lock."""
    if not shutil.which("pgrep"):
        await asyncio.sleep(1.0)
        return
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await _browser_running(browser):
            await asyncio.sleep(0.4)  # small settle so the remote socket is accepting
            return
        await asyncio.sleep(0.25)


@skill(
    name="open_url",
    description=(
        "Open a web URL/website (http/https) in the user's default browser — e.g. 'open "
        "github.com'. Reuses the already-open browser window (adds a tab) instead of "
        "spawning a new window each time. Requires an actual URL; to launch the browser "
        "app itself ('open firefox') use open_app. For DOING something on a page — "
        "reading, clicking, logging-in flows — use browse_url / web_task / browser_open."
    ),
    auto_confirm=True,
    timeout_s=20,  # a cold browser start can take a while; 5s was too tight
)
async def open_url(url: str) -> dict[str, Any]:
    """Open a URL in the default browser, reusing an open window as a new tab.

    url: An http:// or https:// URL.
    """
    if not _is_safe_url(url):
        return {"error": "only http(s) URLs are allowed", "url": url}
    browser = _default_browser()
    if not browser:  # no resolvable browser binary — fall back to plain xdg-open
        if not shutil.which("xdg-open"):
            return {"error": "no browser found (install a browser or xdg-open)", "url": url}
        code, note = await _launch_detached(["xdg-open", url])
        if code not in (0, None):
            return {"error": note or "xdg-open failed", "url": url}
        return {"status": "ok", "url": url}
    # One launcher at a time, so a burst of calls can't each cold-start a new window.
    async with _BROWSER_LAUNCH_LOCK:
        if await _browser_running(browser):
            code, note = await _launch_detached(_reuse_cmd(browser, url))
            if code not in (0, None):  # direct reuse failed → best-effort xdg-open
                code, note = await _launch_detached(["xdg-open", url])
                if code not in (0, None):
                    return {"error": note or "could not open url", "url": url}
            return {"status": "ok", "url": url, "browser": Path(browser).name, "reused": True}
        # Cold start: open the browser once, then wait until it's up so the next call
        # reuses THIS window. None = handed off, still warming (success).
        code, note = await _launch_detached([browser, url])
        if code not in (0, None):
            return {"error": note or "could not launch browser", "url": url}
        await _wait_until_running(browser)
        return {"status": "ok", "url": url, "browser": Path(browser).name, "reused": False}
