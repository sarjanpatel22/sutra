"""Tests for the dashboard server — real HTTP against an ephemeral port.

agent.run is faked (no LLM). Covers the endpoints, the SSE stream end-to-end,
and the confirmation gate driven entirely over HTTP — the way the GUI does it.
"""

import json
import time
import urllib.request

import pytest

import friday.gui.dashboard as dash
from friday.core import agent


@pytest.fixture()
def base(monkeypatch):
    dash._bridge = None  # fresh bridge per test — no replay bleed between tests
    httpd = dash.serve_background(host="127.0.0.1", port=0, open_browser=False)
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    if dash._bridge:
        dash._bridge.stop()
    dash._bridge = None


def get(url, timeout=3):
    return urllib.request.urlopen(url, timeout=timeout)


def post(url, obj, timeout=3):
    req = urllib.request.Request(
        url, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def sse_until(base_url, kind, timeout=6.0):
    """Read the live SSE stream until an event of `kind`; return all events seen."""
    seen = []
    deadline = time.monotonic() + timeout
    with get(base_url + "/api/events", timeout=timeout) as resp:
        while time.monotonic() < deadline:
            raw = resp.readline().decode()
            if not raw.startswith("data: "):
                continue
            e = json.loads(raw[6:])
            seen.append(e)
            if e["type"] == kind:
                return seen
    raise AssertionError(f"no {kind!r} on SSE within {timeout}s; saw {[e['type'] for e in seen]}")


def test_pages_and_read_apis(base):
    assert b"CONVERSATION" in get(base + "/").read()
    assert b"THREE" in get(base + "/orb").read()  # the orb rides along for the iframe
    assert b"SUTRA" in get(base + "/sutra-browser").read()  # splash for her auto-browser
    skills = json.load(get(base + "/api/skills"))
    by_name = {s["name"]: s for s in skills}
    assert len(skills) > 20
    assert by_name["write_file"]["gated"] is True  # gate posture surfaces in the GUI
    stats = json.load(get(base + "/api/stats"))
    assert "text_by_tier" in stats and "budget" in stats
    assert json.load(get(base + "/api/state"))["state"] in ("idle", "listening", "speaking")


def test_chat_roundtrip_over_http(base, monkeypatch):
    async def fake(text, **k):
        return f"echo:{text}", []

    monkeypatch.setattr(agent, "run", fake)
    code, out = post(base + "/api/chat", {"text": "hello orb", "tier": "medium"})
    assert code == 202 and out["accepted"] is True
    events = sse_until(base, "reply")
    assert events[-1]["text"] == "echo:hello orb"


def test_confirm_gate_over_http(base, monkeypatch):
    async def fake(text, history=None, tier=None, confirm=None, observe=None):
        ok = await confirm("run_command", {"command": "uptime"})
        return f"gate:{ok}", []

    monkeypatch.setattr(agent, "run", fake)
    post(base + "/api/chat", {"text": "run uptime"})
    req = sse_until(base, "confirm_request")[-1]
    assert req["skill"] == "run_command"
    code, out = post(base + "/api/confirm", {"confirm_id": req["confirm_id"], "approve": True})
    assert code == 200 and out["approved"] is True
    assert sse_until(base, "reply")[-1]["text"] == "gate:True"


def test_bad_requests(base):
    code, out = post(base + "/api/chat", {"text": "   "})
    assert code == 400 and "empty" in out["error"]
    code, out = post(base + "/api/confirm", {"confirm_id": "404", "approve": True})
    assert code == 400 and "no pending" in out["error"]
    req = urllib.request.Request(
        base + "/api/chat", data=b"not json",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=3)
    assert err.value.code == 400
