"""Tests for the memory-graph viewer HTTP endpoints — /api/graph, /api/recall, /api/facts.

Drives the real stdlib handler on an ephemeral port with the data sources stubbed, so
the JSON shape (and the DB-down degradation) is locked without touching FalkorDB.
"""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

import pytest

from friday.memory import graphview


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(
        graphview, "fetch_graph",
        lambda *a, **k: {"nodes": [{"id": "1"}], "links": [], "stats": {"nodes": 1}},
    )
    # recall_scored / all_facts are imported lazily inside do_GET, so patch them on
    # their source module — the in-handler import picks up the stub at call time.
    monkeypatch.setattr(
        "friday.memory.recall_index.recall_scored",
        lambda q, k=8: [("Sarjan works at Acme Corp", 0.91), ("Sutra is voice-native", 0.80)],
    )
    monkeypatch.setattr("friday.memory.recall_index.all_facts", lambda: ["b fact", "a fact"])

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), graphview._Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(url: str) -> tuple[int, dict]:
    with urlopen(url, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_api_graph_ok(server):
    status, body = _get(f"{server}/api/graph")
    assert status == 200
    assert body["stats"]["nodes"] == 1


def test_api_recall_returns_ranked_hits(server):
    status, body = _get(f"{server}/api/recall?q=who+does+sarjan+work+for&k=2")
    assert status == 200
    assert body["query"] == "who does sarjan work for"
    assert [h["fact"] for h in body["hits"]] == [
        "Sarjan works at Acme Corp", "Sutra is voice-native",
    ]
    assert body["hits"][0]["score"] == 0.91


def test_api_recall_empty_query_returns_no_hits(server):
    status, body = _get(f"{server}/api/recall?q=")
    assert status == 200
    assert body["hits"] == []


def test_api_facts_are_sorted(server):
    status, body = _get(f"{server}/api/facts")
    assert status == 200
    assert body["facts"] == ["a fact", "b fact"]
    assert body["count"] == 2


def test_api_graph_degrades_when_db_down(server, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("falkordb down")

    monkeypatch.setattr(graphview, "fetch_graph", boom)
    status, body = _get(f"{server}/api/graph")
    assert status == 200  # surfaced to the UI as an error payload, not an HTTP 500
    assert body["nodes"] == [] and body["links"] == []
    assert "error" in body
