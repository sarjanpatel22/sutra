"""Tests for the dashboard bridge — event bus, chat turn, confirm round-trip.

agent.run is faked (no LLM); what's under test is the seam: thread-safe pub/sub,
HTTP-thread → asyncio-loop submission, and the confirmation Future round-trip
that keeps the gate's no-approval-no-execution semantics in the GUI.
"""

import queue
import time

import pytest

import friday.gui.bridge as bridge_mod
from friday.gui.bridge import Bridge, EventBus


@pytest.fixture()
def bridge(monkeypatch):
    monkeypatch.setattr(bridge_mod, "set_state", lambda *a, **k: None)
    b = Bridge()
    b.start()
    yield b
    b.stop()


def drain_until(q, kind, timeout=5.0):
    """Collect events until one of `kind` arrives; fail loudly on timeout."""
    seen = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            e = q.get(timeout=0.2)
        except queue.Empty:
            continue
        seen.append(e)
        if e["type"] == kind:
            return seen
    raise AssertionError(f"no {kind!r} event within {timeout}s; saw {[e['type'] for e in seen]}")


# -- EventBus ----------------------------------------------------------------

def test_bus_fanout_and_replay():
    bus = EventBus()
    early = bus.subscribe()
    bus.publish("user", text="hello")
    late = bus.subscribe()  # joins after the event — replay must cover it
    assert early.get(timeout=1)["text"] == "hello"
    assert late.get(timeout=1)["text"] == "hello"
    bus.unsubscribe(early)
    bus.publish("reply", text="bye")
    assert late.get(timeout=1)["type"] == "reply"
    assert early.empty()  # unsubscribed — no delivery


# -- chat turn ---------------------------------------------------------------

async def _fake_run_ok(text, history=None, tier=None, confirm=None, observe=None):
    if observe:
        observe("tool_start", {"skill": "calc", "args": {}, "gated": False})
        observe("tool_end", {"skill": "calc", "status": "ok"})
    return "pong", (history or []) + [{"role": "user", "content": text}]


def test_chat_turn_publishes_user_tools_reply(bridge, monkeypatch):
    monkeypatch.setattr(bridge_mod.agent, "run", _fake_run_ok)
    q = bridge.bus.subscribe()
    out = bridge.submit_chat("ping", tier="medium")
    assert out == {"accepted": True, "tier": "medium"}
    kinds = [e["type"] for e in drain_until(q, "reply")]
    assert kinds == ["user", "status", "tool_start", "tool_end", "reply"]


def test_rejects_empty_bad_tier_and_busy(bridge, monkeypatch):
    import asyncio

    async def slow(text, **k):
        await asyncio.sleep(1.0)
        return "done", []

    monkeypatch.setattr(bridge_mod.agent, "run", slow)
    assert "empty" in bridge.submit_chat("  ")["error"]
    assert "unknown tier" in bridge.submit_chat("hi", tier="warp")["error"]
    q = bridge.bus.subscribe()
    assert bridge.submit_chat("hi")["accepted"] is True
    assert "busy" in bridge.submit_chat("again")["error"]  # one turn at a time
    drain_until(q, "reply")  # then it frees up
    assert bridge.submit_chat("third")["accepted"] is True


# -- confirmation gate ---------------------------------------------------------

async def _fake_run_gated(text, history=None, tier=None, confirm=None, observe=None):
    ok = await confirm("write_file", {"path": "/tmp/x"})
    return f"approved={ok}", []


@pytest.mark.parametrize("approve", [True, False])
def test_confirm_roundtrip(bridge, monkeypatch, approve):
    monkeypatch.setattr(bridge_mod.agent, "run", _fake_run_gated)
    q = bridge.bus.subscribe()
    bridge.submit_chat("write something")
    req = drain_until(q, "confirm_request")[-1]
    assert req["skill"] == "write_file" and req["args"] == {"path": "/tmp/x"}
    assert bridge.resolve_confirm(req["confirm_id"], approve)["resolved"] is True
    events = drain_until(q, "reply")
    assert events[-1]["text"] == f"approved={approve}"
    assert any(e["type"] == "confirm_result" and e["approved"] is approve for e in events)


def test_confirm_timeout_declines(bridge, monkeypatch):
    monkeypatch.setattr(bridge_mod.agent, "run", _fake_run_gated)
    monkeypatch.setattr(bridge_mod, "CONFIRM_TIMEOUT_S", 0.3)
    q = bridge.bus.subscribe()
    bridge.submit_chat("write something")
    events = drain_until(q, "reply")  # nobody clicks
    assert events[-1]["text"] == "approved=False"  # fail-safe: silence = decline
    assert any(e["type"] == "confirm_result" and e.get("timeout") for e in events)


def test_resolve_unknown_confirm_errors(bridge):
    assert "no pending confirmation" in bridge.resolve_confirm("999", True)["error"]


def test_agent_error_is_surfaced_not_fatal(bridge, monkeypatch):
    async def boom(text, **k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(bridge_mod.agent, "run", boom)
    q = bridge.bus.subscribe()
    bridge.submit_chat("hi")
    events = drain_until(q, "error")
    assert "kaput" in events[-1]["text"]
    monkeypatch.setattr(bridge_mod.agent, "run", _fake_run_ok)
    assert bridge.submit_chat("recovered")["accepted"] is True  # inflight was cleared
