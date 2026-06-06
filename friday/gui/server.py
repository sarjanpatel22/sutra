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


def serve(host: str = "127.0.0.1", port: int = 8710, open_browser: bool = True) -> None:
    """Start the orb server (blocking). Ctrl+C to stop."""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    url = f"http://{host}:{port}/"
    print(f"\n  ☀  Sutra HORIZON orb → {url}")
    print("      standalone (state: idle) · Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.server_close()
