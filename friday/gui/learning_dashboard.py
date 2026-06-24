"""Sutra — Learning dashboard server.

Serves learning.html and /api/learning (the snapshot the page polls every 5s). Same stdlib-serve
discipline as the orb: one ThreadingHTTPServer, vanilla page, no Node toolchain. Read-only — it
only reflects what `friday.core.learning.snapshot_dict()` reports.

  sutra learning --web        # open the live browser dashboard
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_HTML = Path(__file__).with_name("learning.html")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # silence the stdlib access log
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
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
                self._send(500, b"learning.html missing", "text/plain")
            return
        if path == "/api/learning":
            from friday.core.learning import snapshot_dict
            try:
                body = json.dumps(snapshot_dict()).encode()
            except Exception as e:  # noqa: BLE001 — never crash the dashboard on a data hiccup
                body = json.dumps({"error": str(e)}).encode()
            self._send(200, body, "application/json")
            return
        self._send(404, b"not found", "text/plain")


def serve_background(host: str = "127.0.0.1", port: int = 8721, *,
                     open_browser: bool = True) -> ThreadingHTTPServer:
    """Start the dashboard in a daemon thread; return the server (for embedding/tests)."""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    if open_browser:
        try:
            webbrowser.open(f"http://{host}:{httpd.server_address[1]}/")
        except Exception:  # noqa: BLE001 — headless / no browser → serve anyway
            pass
    return httpd


def serve(host: str = "127.0.0.1", port: int = 8721, *, open_browser: bool = True) -> None:
    """Serve the learning dashboard in the foreground until Ctrl+C."""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    if open_browser:
        try:
            webbrowser.open(f"http://{host}:{httpd.server_address[1]}/")
        except Exception:  # noqa: BLE001 — headless / no browser → serve anyway
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
