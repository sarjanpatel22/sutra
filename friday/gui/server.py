"""Serve the HORIZON orb — Sutra's first on-screen presence.

Zero web deps: stdlib http.server serves orb.html (Three.js loads from CDN), plus
a tiny /api/state endpoint the orb polls. Mirrors friday/memory/graphview.serve so
the whole GUI stays on the stdlib-serve path until the Tauri/Next.js dashboard
phase actually starts (do NOT pull in the Node toolchain here).

State is process-local and starts "idle". The voice loop can call set_state(...)
later to make the orb pulse with TTS amplitude — that's the next slice; standalone
the orb just drifts.

Run:  sutra orb            # serves on http://localhost:8710 and opens a browser
      sutra orb --no-open  # just serve
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_HTML = Path(__file__).with_name("orb.html")

# Process-local presence state, read by the orb every ~250ms. amplitude 0..1 is
# only meaningful while speaking (drives the brightness pulse).
_state: dict[str, Any] = {"state": "idle", "amplitude": 0.0}
_lock = threading.Lock()


def set_state(state: str, amplitude: float = 0.0) -> None:
    """Update what the orb shows: state in {idle, listening, speaking}."""
    with _lock:
        _state["state"] = state
        _state["amplitude"] = max(0.0, min(1.0, amplitude))


def get_state() -> dict[str, Any]:
    with _lock:
        return dict(_state)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # silence stdlib access log
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                self._send(200, _HTML.read_bytes(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, b"orb.html missing", "text/plain")
            return
        if path == "/api/state":
            self._send(200, json.dumps(get_state()).encode(), "application/json")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 (stdlib API)
        """Let another process (e.g. the voice loop) drive the orb's state.

        POST /api/state  {"state": "speaking", "amplitude": 0.7}
        This is the cross-process wiring point for voice reactivity — the orb runs
        as its own `sutra orb` process, so callers push state over HTTP, not in-mem.
        """
        if self.path.split("?")[0] != "/api/state":
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            set_state(str(body.get("state", "idle")), float(body.get("amplitude", 0.0)))
            self._send(200, json.dumps(get_state()).encode(), "application/json")
        except Exception as e:  # noqa: BLE001 — never crash the server on bad input
            self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")


# Chromium/Chrome in --app mode = a borderless, chrome-less window: a native-feeling
# ambient display, no browser UI, no Tauri toolchain. Falls back to a normal browser
# tab if no Chromium-family browser is found.
_KIOSK_BROWSERS = (
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "brave-browser"
)


def _launch_fullscreen(url: str) -> subprocess.Popen[bytes] | None:
    """Open url full-screen (borderless --app) in a Chromium-family browser. None if none.

    Hard-won flags for a *stable* ambient window:
      - isolated --user-data-dir: a second launch against the default profile would
        hand off to the running instance and immediately exit.
      - disable OptimizationGuideOnDeviceModel: on a box with no usable GPU adapter,
        chromium's on-device-model crash-loops the GPU process until the window dies.
      - --disable-gpu-process-crash-limit: don't bail if the GPU process blips.
      - no --kiosk: it exits on stray focus/key events; --app is already borderless.
    """
    exe = next((shutil.which(b) for b in _KIOSK_BROWSERS if shutil.which(b)), None)
    if not exe:
        return None
    profile = Path(tempfile.gettempdir()) / "sutra-orb-profile"
    return subprocess.Popen(
        [
            exe, f"--app={url}", "--start-fullscreen", "--window-position=0,0",
            f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", "--noerrdialogs",
            "--disable-translate", "--disable-infobars", "--disable-session-crashed-bubble",
            "--disable-features=Translate,OptimizationGuideOnDeviceModel,"
            "OptimizationGuideModelDownloading,OptimizationHints",
            "--disable-gpu-process-crash-limit", "--enable-unsafe-swiftshader",
            "--overscroll-history-navigation=0",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def serve(
    host: str = "127.0.0.1", port: int = 8710,
    open_browser: bool = True, fullscreen: bool = True,
) -> None:
    """Start the orb server (blocking). Ctrl+C to stop.

    fullscreen=True launches a Chromium-family browser in kiosk/app mode (full-screen
    ambient, no chrome); set it False for a normal browser tab.
    """
    httpd = ThreadingHTTPServer((host, port), _Handler)
    url = f"http://{host}:{port}/"
    print(f"\n  ✦  Sutra HORIZON orb → {url}")
    if open_browser:
        def _open() -> None:
            if not (fullscreen and _launch_fullscreen(url)):
                webbrowser.open(url)  # no kiosk browser, or windowed requested
        threading.Timer(0.6, _open).start()
        print("      full-screen ambient · alt+f4 (or close the window) to exit.\n"
              if fullscreen else "      windowed · Ctrl+C to stop.\n")
    else:
        print("      serving only (no window) · Ctrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.server_close()


def serve_background(
    host: str = "127.0.0.1", port: int = 8710,
    open_browser: bool = True, fullscreen: bool = True,
) -> tuple[ThreadingHTTPServer | None, subprocess.Popen[bytes] | None]:
    """Start the orb server in a daemon thread (non-blocking) and open the window.

    Returns (httpd, browser_proc) so the caller can httpd.shutdown() / proc.terminate()
    on exit. Used by `sutra voice --orb`: the orb runs in the same process as the voice
    loop (orb_push POSTs to this loopback server). If the port is already served (a
    standalone `sutra orb`), httpd is None and that existing server is reused.
    """
    httpd: ThreadingHTTPServer | None = None
    try:
        httpd = ThreadingHTTPServer((host, port), _Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
    except OSError:
        pass  # already serving on this port — reuse it for the window
    url = f"http://{host}:{port}/"
    proc: subprocess.Popen[bytes] | None = None
    if open_browser:
        proc = _launch_fullscreen(url) if fullscreen else None
        if proc is None:
            webbrowser.open(url)
    return httpd, proc
