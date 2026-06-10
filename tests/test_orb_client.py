"""Tests for orb voice-reactivity: the push client + the TTS loudness envelope."""

import threading
import time
from http.server import ThreadingHTTPServer

import numpy as np

from friday.gui import orb_client, server
from friday.voice.pipeline import _ORB_AMP_SCALE, _chunk_envelope, _drive_orb_speaking

SR = 24000  # Kokoro sample rate


def test_push_delivers_to_server_over_http():
    """push() should reach the orb server's /api/state via the background worker."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    server.set_state("idle", 0.0)
    orb_client._URL = f"http://127.0.0.1:{port}/api/state"
    try:
        orb_client.push("speaking", 0.5)
        deadline = time.time() + 6.0  # generous: covers a worst-case worker back-off
        while time.time() < deadline and server.get_state()["state"] != "speaking":
            time.sleep(0.05)
        assert server.get_state() == {"state": "speaking", "amplitude": 0.5}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_push_clamps_and_updates_latest():
    orb_client._URL = "http://127.0.0.1:9/api/state"  # dead port → worker no-ops quietly
    orb_client.push("speaking", 2.0)
    assert orb_client._latest == {"state": "speaking", "amplitude": 1.0}
    orb_client.push("listening", -3.0)
    assert orb_client._latest["amplitude"] == 0.0


def test_chunk_envelope_traces_loudness():
    """Constant-loudness float chunk → flat envelope at rms*scale, ~25 windows/sec."""
    chunk = np.full(SR, 0.1, dtype=np.float32)  # 1s, RMS 0.1
    env = _chunk_envelope(chunk, SR)
    assert len(env) == 24  # ~25 windows per second (final partial window dropped)
    assert all(abs(a - 0.1 * _ORB_AMP_SCALE) < 1e-3 for a in env)


def test_chunk_envelope_normalises_int16():
    chunk = np.full(SR, 3277, dtype=np.int16)  # ≈ 0.1 * 32768
    env = _chunk_envelope(chunk, SR)
    assert abs(env[0] - 0.1 * _ORB_AMP_SCALE) < 0.05  # int16 normalised to [-1, 1]


def test_chunk_envelope_silence_and_clip():
    assert all(a == 0.0 for a in _chunk_envelope(np.zeros(SR, np.float32), SR))
    loud = np.full(SR, 0.9, np.float32)  # 0.9 * 7 = 6.3 → clamped
    assert all(a == 1.0 for a in _chunk_envelope(loud, SR))


def test_drive_orb_speaking_never_raises_on_empty():
    _drive_orb_speaking(np.array([], np.float32), SR)  # empty → no thread, no raise
    _drive_orb_speaking(None, SR)  # None → no raise
