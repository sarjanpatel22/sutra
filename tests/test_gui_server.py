"""Tests for the HORIZON orb server's state contract (no socket needed)."""

from friday.gui import server


def test_set_state_clamps_amplitude():
    server.set_state("speaking", 2.5)
    assert server.get_state() == {"state": "speaking", "amplitude": 1.0}
    server.set_state("listening", -1.0)
    assert server.get_state()["amplitude"] == 0.0
    server.set_state("idle")  # default amplitude 0.0
    assert server.get_state() == {"state": "idle", "amplitude": 0.0}


def test_get_state_returns_a_copy():
    server.set_state("idle", 0.0)
    snap = server.get_state()
    snap["state"] = "tampered"
    assert server.get_state()["state"] == "idle"  # internal state untouched


def test_orb_html_exists_and_references_state_endpoint():
    html = (server._HTML).read_text()
    assert "THREE" in html and "/api/state" in html  # serves the right page
