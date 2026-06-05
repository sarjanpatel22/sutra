"""Tests for the keyring secret broker (keyring → env → default resolution)."""

import os

import keyring

from friday.core import secrets


def test_env_fallback_when_keyring_empty(monkeypatch):
    monkeypatch.setattr(keyring, "get_password", lambda s, n: None)
    monkeypatch.setenv("FOO_KEY", "from_env")
    assert secrets.get_secret("FOO_KEY") == "from_env"


def test_keyring_wins_over_env(monkeypatch):
    monkeypatch.setattr(
        keyring, "get_password", lambda s, n: "from_keyring" if n == "FOO_KEY" else None
    )
    monkeypatch.setenv("FOO_KEY", "from_env")
    assert secrets.get_secret("FOO_KEY") == "from_keyring"


def test_default_when_neither(monkeypatch):
    monkeypatch.setattr(keyring, "get_password", lambda s, n: None)
    monkeypatch.delenv("NOPE_KEY", raising=False)
    assert secrets.get_secret("NOPE_KEY", "d") == "d"


def test_status_reports_source(monkeypatch):
    monkeypatch.setattr(
        keyring, "get_password", lambda s, n: "x" if n == "ANTHROPIC_API_KEY" else None
    )
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    st = secrets.status()
    assert st["ANTHROPIC_API_KEY"] == "keyring"
    assert st["GROQ_API_KEY"] == "env"
    assert st["GEMINI_API_KEY"] == "missing"


def test_load_into_env_does_not_override(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "env_val")  # recorded → restored on teardown
    monkeypatch.setattr(keyring, "get_password", lambda s, n: "kr_val")
    secrets.load_into_env(("GROQ_API_KEY",))
    assert os.environ["GROQ_API_KEY"] == "env_val"


def test_load_into_env_fills_missing(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)  # recorded → restored on teardown
    monkeypatch.setattr(keyring, "get_password", lambda s, n: "kr_val")
    n = secrets.load_into_env(("GROQ_API_KEY",))
    assert n == 1 and os.environ.get("GROQ_API_KEY") == "kr_val"
