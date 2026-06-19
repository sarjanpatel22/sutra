"""Tests for the ambient daemon's config + privacy guardrails (friday/ambient/config.py).

The is_sensitive / is_noise / should_track_file filters decide what the always-on
OS-memory daemon is allowed to read and record — so a regression here could leak SSH
keys, .env secrets, or passwords into memory. These lock that boundary down."""

from __future__ import annotations

from pathlib import Path

import pytest

from friday.ambient import config

# --- _flag -----------------------------------------------------------------

def test_flag_unset_uses_default(monkeypatch):
    monkeypatch.delenv("AMB_TEST_FLAG", raising=False)
    assert config._flag("AMB_TEST_FLAG", True) is True
    assert config._flag("AMB_TEST_FLAG", False) is False


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("yes", True), ("on", True), ("anything", True),
    ("True", True), ("YES", True),
    ("0", False), ("false", False), ("no", False), ("False", False), ("NO", False),
    ("", False),        # explicitly-empty is falsy (the hardening)
    ("   ", False),     # whitespace-only too
])
def test_flag_parses_values(monkeypatch, val, expected):
    monkeypatch.setenv("AMB_TEST_FLAG", val)
    assert config._flag("AMB_TEST_FLAG", True) is expected


def test_empty_flag_does_not_enable_deletion_toggle(monkeypatch):
    # The concrete safety case: an empty value must NOT turn on narration sweeping.
    monkeypatch.setenv("AMBIENT_SWEEP_NARRATION", "")
    assert config._flag("AMBIENT_SWEEP_NARRATION", False) is False


# --- is_sensitive (privacy denylist) ---------------------------------------

@pytest.mark.parametrize("path", [
    "/home/u/.ssh/id_rsa",
    "/home/u/.gnupg/secring",
    "/home/u/.aws/credentials",
    "/home/u/.kube/config",
    "/home/u/.docker/config.json",
    "/home/u/.password-store/x",
    "/home/u/project/.env",
    "/home/u/project/.env.local",
    "/home/u/secret_notes.txt",
    "/home/u/my-password.txt",
    "/home/u/api_token.json",
    "/home/u/server.pem",
    "/home/u/private.key",
    "/home/u/id_rsa",
])
def test_sensitive_paths_are_blocked(path):
    assert config.is_sensitive(Path(path)) is True


@pytest.mark.parametrize("path", [
    "/home/u/.SSH/id",          # dir match is case-insensitive
    "/home/u/PASSWORD.txt",     # name hint is case-insensitive
    "/home/u/Project/.Env",
])
def test_sensitive_match_is_case_insensitive(path):
    assert config.is_sensitive(Path(path)) is True


@pytest.mark.parametrize("path", [
    "/home/u/projects/app/main.py",
    "/home/u/Documents/notes.md",
    "/home/u/friday-os/README.md",
])
def test_ordinary_paths_are_not_sensitive(path):
    assert config.is_sensitive(Path(path)) is False


# --- is_noise --------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/home/u/app/__pycache__/x.pyc",
    "/home/u/app/node_modules/lib/index.js",
    "/home/u/app/.venv/bin/python",
    "/home/u/app/.git/HEAD",
    "/home/u/app/dist/bundle.js",
])
def test_noise_dirs_are_filtered(path):
    assert config.is_noise(Path(path)) is True


@pytest.mark.parametrize("path", [
    "/home/u/app/main.pyc",
    "/home/u/app/run.log",
    "/home/u/app/scratch.tmp",
    "/home/u/app/x.swp",
])
def test_noise_suffixes_are_filtered(path):
    assert config.is_noise(Path(path)) is True


def test_real_source_file_is_not_noise():
    assert config.is_noise(Path("/home/u/app/main.py")) is False


# --- should_track_file (the combined gate) ---------------------------------

def test_should_track_ordinary_source_file():
    assert config.should_track_file(Path("/home/u/projects/app/main.py")) is True


def test_should_not_track_sensitive_even_if_not_noise():
    assert config.should_track_file(Path("/home/u/.ssh/config")) is False


def test_should_not_track_noise_even_if_not_sensitive():
    assert config.should_track_file(Path("/home/u/app/__pycache__/x.pyc")) is False


# --- _watch_dirs -----------------------------------------------------------

def test_watch_dirs_env_override_parses_colon_list(monkeypatch):
    monkeypatch.setenv("AMBIENT_WATCH_DIRS", "/tmp/a:/tmp/b")
    assert config._watch_dirs() == [Path("/tmp/a"), Path("/tmp/b")]


def test_watch_dirs_env_override_skips_blanks(monkeypatch):
    monkeypatch.setenv("AMBIENT_WATCH_DIRS", "/tmp/a::  :/tmp/b")
    assert config._watch_dirs() == [Path("/tmp/a"), Path("/tmp/b")]


def test_watch_dirs_default_returns_only_existing(monkeypatch, tmp_path):
    monkeypatch.delenv("AMBIENT_WATCH_DIRS", raising=False)
    monkeypatch.setattr(config, "HOME", tmp_path)
    (tmp_path / "projects").mkdir()  # only this candidate exists
    dirs = config._watch_dirs()
    assert tmp_path / "projects" in dirs
    assert tmp_path / "friday-os" not in dirs  # doesn't exist → excluded
