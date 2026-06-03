"""Brain-graph viewer for the Sutra memory layer.

Serves a self-contained WebGL visualization of the Graphiti/FalkorDB knowledge
graph: a glowing neural cloud where Entities, Episodes, and Communities are nodes
and RELATES_TO / MENTIONS / HAS_EPISODE edges fire travelling particles between
them. Zero external web deps — stdlib http.server + the (already-installed)
FalkorDB client. Frontend libs (three.js + 3d-force-graph) load from CDN.

Run:  sutra graph            # serves on http://localhost:8700 and opens a browser
      sutra graph --no-open  # just serve
"""

from __future__ import annotations

import json
import logging
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_HTML = Path(__file__).with_name("graphview.html")

_FALKOR_HOST = os.getenv("FALKORDB_HOST", "localhost")
_FALKOR_PORT = int(os.getenv("FALKORDB_PORT", "6379"))
_GRAPH = os.getenv("MEMORY_GROUP_ID", "sarjan")

# Visual identity per node label — colour + relative size. The frontend reads
# these so the palette stays defined in one place (here).
_LABEL_STYLE: dict[str, dict[str, Any]] = {
    "Entity": {"color": "#5eead4", "val": 6, "group": "entity"},
    "Episodic": {"color": "#a78bfa", "val": 4, "group": "episode"},
    "Community": {"color": "#f0abfc", "val": 9, "group": "community"},
    "Saga": {"color": "#fbbf24", "val": 9, "group": "saga"},
}
_DEFAULT_STYLE = {"color": "#94a3b8", "val": 4, "group": "other"}


def fetch_graph(graph_name: str = _GRAPH) -> dict[str, Any]:
    """Query FalkorDB and return a {nodes, links, stats} dict for the frontend."""
    from falkordb import FalkorDB

    db = FalkorDB(host=_FALKOR_HOST, port=_FALKOR_PORT)
    g = db.select_graph(graph_name)

    nodes: dict[str, dict[str, Any]] = {}
    links: list[dict[str, Any]] = []

    # --- Nodes ---------------------------------------------------------------
    res = g.query(
        "MATCH (n) RETURN id(n), labels(n), "
        "coalesce(n.name, n.content, '') AS label, "
        "coalesce(n.summary, n.content, '') AS summary"
    )
    for nid, labels, label, summary in res.result_set:
        lbl = labels[0] if labels else "Other"
        style = _LABEL_STYLE.get(lbl, _DEFAULT_STYLE)
        # Episodes carry an internal "smoke-test-<ts>" name; show their text instead.
        if lbl == "Episodic":
            display = summary or label
            name = (display[:48] + "…") if len(display) > 49 else display
        else:
            name = label or lbl
        nodes[str(nid)] = {
            "id": str(nid),
            "name": name,
            "summary": summary or "",
            "label": lbl,
            "color": style["color"],
            "val": style["val"],
            "group": style["group"],
        }

    # --- Edges ---------------------------------------------------------------
    res = g.query(
        "MATCH (a)-[r]->(b) RETURN id(a), id(b), type(r), "
        "coalesce(r.fact, r.name, '') AS fact"
    )
    for src, dst, rtype, fact in res.result_set:
        s, d = str(src), str(dst)
        if s not in nodes or d not in nodes:
            continue
        links.append({"source": s, "target": d, "type": rtype, "fact": fact or rtype})

    label_counts: dict[str, int] = {}
    for n in nodes.values():
        label_counts[n["label"]] = label_counts.get(n["label"], 0) + 1

    return {
        "nodes": list(nodes.values()),
        "links": links,
        "stats": {
            "graph": graph_name,
            "nodes": len(nodes),
            "links": len(links),
            "labels": label_counts,
        },
    }


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
                self._send(500, b"graphview.html missing", "text/plain")
            return
        if path == "/api/graph":
            try:
                data = fetch_graph()
                self._send(200, json.dumps(data).encode(), "application/json")
            except Exception as e:  # noqa: BLE001 — surface DB-down to the UI
                payload = {"error": str(e), "nodes": [], "links": [], "stats": {}}
                self._send(200, json.dumps(payload).encode(), "application/json")
            return
        self._send(404, b"not found", "text/plain")


def serve(host: str = "127.0.0.1", port: int = 8700, open_browser: bool = True) -> None:
    """Start the viewer server (blocking). Ctrl+C to stop."""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    url = f"http://{host}:{port}/"
    print(f"\n  🧠  Sutra memory graph → {url}")
    print(f"      graph='{_GRAPH}'  falkordb={_FALKOR_HOST}:{_FALKOR_PORT}")
    print("      Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.server_close()
