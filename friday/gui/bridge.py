"""Dashboard bridge — the seam between the stdlib HTTP server and the async agent.

The dashboard server (threaded, sync) and the agent loop (asyncio) meet here:
a Bridge owns one asyncio loop in a daemon thread; HTTP handlers submit chat
turns onto it and resolve confirmation requests from it. Every observable
moment (user turn, tool start/end, confirmation ask/answer, reply) is fanned
out on an EventBus that SSE clients drain — so the GUI shows the agent loop
*live*, including the confirmation gate, which keeps its exact semantics:
no approval, no execution.
"""

from __future__ import annotations

import asyncio
import itertools
import queue
import threading
import time
from collections import deque
from typing import Any

from friday.core import agent
from friday.core.router import Tier
from friday.gui.server import set_state

_REPLAY = 80  # events replayed to a newly connected client (page reload survives)
_QUEUE_MAX = 500
CONFIRM_TIMEOUT_S = 120.0  # unanswered confirm card → declined (fail-safe)
_MAX_HISTORY = 24
_SPEAK_PULSE_S = 2.5  # how long the orb glows "speaking" after a reply lands


class EventBus:
    """Thread-safe pub/sub with a replay ring for late subscribers."""

    def __init__(self) -> None:
        self._subs: list[queue.Queue[dict[str, Any]]] = []
        self._replay: deque[dict[str, Any]] = deque(maxlen=_REPLAY)
        self._lock = threading.Lock()
        self._seq = itertools.count(1)

    def publish(self, kind: str, **data: Any) -> dict[str, Any]:
        event = {"id": next(self._seq), "type": kind, "ts": round(time.time(), 3), **data}
        with self._lock:
            self._replay.append(event)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:  # slow client — drop for them, never block the loop
                pass
        return event

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_QUEUE_MAX)
        with self._lock:
            for e in self._replay:
                q.put_nowait(e)
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)


class Bridge:
    """One chat session: agent loop + confirmation gate, driven over HTTP."""

    def __init__(self) -> None:
        self.bus = EventBus()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._history: list[dict[str, Any]] = []
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._confirm_ids = itertools.count(1)
        self._inflight = threading.Event()

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._loop:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="sutra-dashboard-loop", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._loop:
            loop = self._loop
            try:  # close Sutra's browser if a web task opened it (best-effort)
                from friday.skills.browser_session import close_session

                asyncio.run_coroutine_threadsafe(close_session(), loop).result(timeout=8)
            except Exception:  # noqa: BLE001 — never block shutdown on browser teardown
                pass

            def _cancel_all() -> None:
                for task in asyncio.all_tasks(loop):
                    task.cancel()

            loop.call_soon_threadsafe(_cancel_all)
            loop.call_soon_threadsafe(loop.stop)
            if self._thread:
                self._thread.join(timeout=3)
            self._loop, self._thread = None, None

    # -- chat ------------------------------------------------------------
    def submit_chat(self, text: str, tier: str = "medium") -> dict[str, Any]:
        """Queue one agent turn. Called from HTTP handler threads."""
        text = text.strip()
        if not text:
            return {"error": "empty message"}
        if not self._loop:
            return {"error": "bridge not started"}
        if self._inflight.is_set():
            return {"error": "busy — a turn is already running"}
        try:
            t = Tier(tier)
        except ValueError:
            return {"error": f"unknown tier: {tier}"}
        self._inflight.set()
        self.bus.publish("user", text=text, tier=t.value)
        asyncio.run_coroutine_threadsafe(self._turn(text, t), self._loop)
        return {"accepted": True, "tier": t.value}

    async def _turn(self, text: str, tier: Tier) -> None:
        set_state("listening", 0.35)  # orb leans in while she thinks
        self.bus.publish("status", phase="thinking")
        try:
            reply, self._history = await agent.run(
                text,
                history=self._history,
                tier=tier,
                confirm=self._confirm,
                observe=lambda kind, data: self.bus.publish(kind, **data),
            )
        except Exception as e:  # noqa: BLE001 — surface, never kill the loop thread
            self.bus.publish("error", text=f"agent error: {e}")
            set_state("idle")
            self._inflight.clear()
            return
        self._history = self._history[-_MAX_HISTORY:]
        self.bus.publish("reply", text=reply)
        set_state("speaking", 0.55)
        assert self._loop is not None
        self._loop.call_later(_SPEAK_PULSE_S, set_state, "idle")
        self._inflight.clear()

    # -- confirmation gate -------------------------------------------------
    async def _confirm(self, skill: str, args: dict[str, Any]) -> bool:
        """agent.run's confirm hook: ask the GUI, wait for the click."""
        assert self._loop is not None
        cid = str(next(self._confirm_ids))
        fut: asyncio.Future[bool] = self._loop.create_future()
        self._pending[cid] = fut
        self.bus.publish("confirm_request", confirm_id=cid, skill=skill, args=args)
        try:
            approved = await asyncio.wait_for(fut, timeout=CONFIRM_TIMEOUT_S)
        except TimeoutError:
            approved = False
            self.bus.publish("confirm_result", confirm_id=cid, approved=False, timeout=True)
        finally:
            self._pending.pop(cid, None)
        return approved

    def resolve_confirm(self, confirm_id: str, approve: bool) -> dict[str, Any]:
        """Approve/decline a pending confirmation. Called from HTTP handler threads."""
        fut = self._pending.get(confirm_id)
        if not fut or not self._loop:
            return {"error": f"no pending confirmation {confirm_id!r}"}

        def _set() -> None:
            if not fut.done():
                fut.set_result(bool(approve))

        self._loop.call_soon_threadsafe(_set)
        self.bus.publish("confirm_result", confirm_id=confirm_id, approved=bool(approve))
        return {"resolved": True, "approved": bool(approve)}
