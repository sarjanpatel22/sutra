"""Desktop interaction skills — clipboard and opening URLs (X11 + Wayland)."""

from __future__ import annotations

import asyncio
import shutil
from typing import Any
from urllib.parse import urlparse

from friday.skills.registry import skill


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
    description="Read the current text contents of the system clipboard.",
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
    description="Copy text to the system clipboard.",
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


@skill(
    name="open_url",
    description=(
        "Open a web URL/website (http/https) in the user's default browser — e.g. 'open "
        "github.com'. Requires an actual URL; to launch the browser app itself ('open "
        "firefox') use open_app. For DOING something on a page — reading, clicking, "
        "logging-in flows — use browse_url / web_task / browser_open instead."
    ),
    auto_confirm=True,
    timeout_s=20,  # xdg-open can block while a cold browser starts; 5s was too tight
)
async def open_url(url: str) -> dict[str, Any]:
    """Open a URL in the default browser.

    url: An http:// or https:// URL.
    """
    if not _is_safe_url(url):
        return {"error": "only http(s) URLs are allowed", "url": url}
    if not shutil.which("xdg-open"):
        return {"error": "xdg-open not available"}
    code, note = await _launch_detached(["xdg-open", url])
    if code not in (0, None):  # None = handed off, browser still warming (fine)
        return {"error": note or "xdg-open failed", "url": url}
    return {"status": "ok", "url": url}
