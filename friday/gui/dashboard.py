"""Serve the Sutra dashboard — the working surface around the presence orb.

Same stdlib-serve discipline as the orb (no Node toolchain): one ThreadingHTTPServer
serves dashboard.html, embeds the untouched orb page as its living backdrop, and
exposes the bridge over five tiny endpoints:

  GET  /             dashboard page          GET /orb        the orb (iframe backdrop)
  GET  /api/events   SSE event stream        GET /api/stats  p50/p95 + spend snapshot
  GET  /api/skills   registry posture        GET /api/state  orb state (shared module)
  POST /api/chat     {text, tier}            POST /api/confirm {confirm_id, approve}

Run:  sutra dashboard            # borderless app window
      sutra dashboard --no-open  # serve only, http://127.0.0.1:8720/
"""

from __future__ import annotations

import json
import queue
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from friday.gui.bridge import Bridge
from friday.gui.server import _launch_fullscreen, get_state, set_state

_DASH_HTML = Path(__file__).with_name("dashboard.html")
_ORB_HTML = Path(__file__).with_name("orb.html")
_HEARTBEAT_S = 15.0  # SSE keepalive comment cadence

_bridge: Bridge | None = None


def get_bridge() -> Bridge:
    global _bridge
    if _bridge is None:
        _bridge = Bridge()
        _bridge.start()
    return _bridge


def _skill_posture() -> list[dict[str, Any]]:
    from friday.skills import registry

    registry.autodiscover()
    out = []
    for name in sorted(registry.list_skills()):
        m = registry.get(name)
        out.append({"name": m.name, "gated": not m.auto_confirm,
                    "description": m.description[:140]})
    return out


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

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                self._send(200, _DASH_HTML.read_bytes(), "text/html; charset=utf-8")
            elif path == "/orb":
                self._send(200, _ORB_HTML.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(200, get_state())
            elif path == "/api/skills":
                self._json(200, _skill_posture())
            elif path == "/api/stats":
                from friday.core.stats import gather_stats

                self._json(200, gather_stats())
            elif path == "/api/events":
                self._stream_events()
            else:
                self._send(404, b"not found", "text/plain")
        except FileNotFoundError:
            self._send(500, b"dashboard asset missing", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-response

    def _stream_events(self) -> None:
        """SSE: replay recent events, then stream live ones until the client leaves."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b"retry: 2000\n\n")
        self.wfile.flush()
        q = get_bridge().bus.subscribe()
        try:
            while True:
                try:
                    event = q.get(timeout=_HEARTBEAT_S)
                    payload = f"data: {json.dumps(event)}\n\n".encode()
                except queue.Empty:
                    payload = b": ping\n\n"  # keepalive comment
                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            get_bridge().bus.unsubscribe(q)

    def do_POST(self) -> None:  # noqa: N802 (stdlib API)
        path = self.path.split("?")[0]
        try:
            body = self._body()
        except Exception as e:  # noqa: BLE001 — malformed client JSON
            self._json(400, {"error": f"bad json: {e}"})
            return
        try:
            if path == "/api/chat":
                out = get_bridge().submit_chat(
                    str(body.get("text", "")), str(body.get("tier", "medium"))
                )
                self._json(202 if out.get("accepted") else 400, out)
            elif path == "/api/confirm":
                out = get_bridge().resolve_confirm(
                    str(body.get("confirm_id", "")), bool(body.get("approve"))
                )
                self._json(200 if out.get("resolved") else 400, out)
            elif path == "/api/state":  # parity with the orb server (voice loop driver)
                set_state(str(body.get("state", "idle")), float(body.get("amplitude", 0.0)))
                self._json(200, get_state())
            else:
                self._send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve_background(
    host: str = "127.0.0.1", port: int = 8720, open_browser: bool = False,
    fullscreen: bool = True,
) -> ThreadingHTTPServer:
    """Start the dashboard server + bridge in daemon threads (tests + embedding)."""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.daemon_threads = True  # SSE handler threads must not block exit
    get_bridge()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    if open_browser:
        url = f"http://{host}:{httpd.server_address[1]}/"
        if not (fullscreen and _launch_fullscreen(url)):
            webbrowser.open(url)
    return httpd


def serve(
    host: str = "127.0.0.1", port: int = 8720,
    open_browser: bool = True, fullscreen: bool = True,
) -> None:
    """Start the dashboard (blocking). Ctrl+C to stop."""
    httpd = serve_background(host, port, open_browser=False)
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"\n  ✦  Sutra dashboard → {url}")
    if open_browser:
        def _open() -> None:
            if not (fullscreen and _launch_fullscreen(url)):
                webbrowser.open(url)
        threading.Timer(0.6, _open).start()
        print("      borderless app window · close it (or Ctrl+C here) to exit.\n"
              if fullscreen else "      windowed · Ctrl+C to stop.\n")
    else:
        print("      serving only (no window) · Ctrl+C to stop.\n")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.shutdown()
        if _bridge:
            _bridge.stop()
