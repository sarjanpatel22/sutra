"""Best-effort push of voice state to the running orb (`sutra orb`).

The orb runs as its own process and polls GET /api/state; the voice loop calls
push(state, amplitude) to drive its reactivity over HTTP. Fully fire-and-forget:
a single background thread coalesces the latest state and POSTs it, so the audio
path never blocks and nothing breaks when no orb is running (POSTs just fail
quietly, with a back-off so we don't hammer when nothing is listening).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from urllib.error import URLError

_URL = os.getenv("SUTRA_ORB_URL", "http://127.0.0.1:8710/api/state")
_latest: dict[str, object] = {"state": "idle", "amplitude": 0.0}
_lock = threading.Lock()
_wake = threading.Event()
_started = False


def push(state: str, amplitude: float = 0.0) -> None:
    """Queue an orb state update (non-blocking, best-effort).

    state in {idle, listening, speaking}; amplitude 0..1 only matters while speaking.
    """
    with _lock:
        _latest["state"] = state
        _latest["amplitude"] = max(0.0, min(1.0, float(amplitude)))
    _wake.set()
    _ensure_worker()


def _ensure_worker() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_worker, daemon=True, name="orb-state").start()


def _worker() -> None:
    fails = 0
    while True:
        _wake.wait()
        _wake.clear()
        with _lock:
            payload = json.dumps(_latest).encode()
        try:
            req = urllib.request.Request(
                _URL, data=payload,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            urllib.request.urlopen(req, timeout=0.3).close()
            fails = 0
        except (URLError, OSError):
            fails = min(fails + 1, 50)  # no orb running — back off, stay quiet
        # coalesce bursts (~25/s while active); back off hard when unreachable
        time.sleep(0.04 if fails == 0 else min(0.5 + fails * 0.2, 5.0))
