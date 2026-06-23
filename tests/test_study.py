"""Tests for `sutra study` — the daily grounded self-improvement pass."""

import json
import types

import pytest
from typer.testing import CliRunner

import friday.cli as cli
from friday.core import study
from friday.core.router import Tier

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(study, "_STUDY_DIR", tmp_path / "study")


def _resp(content):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))])


def _port_scan_trace(goal="pwn"):
    return {"goal": goal, "report": "rooted", "messages": [
        {"role": "user", "content": goal},
        {"role": "tool", "name": "port_scan",
         "content": json.dumps({"host": "10.10.10.5", "open": [{"port": 22, "banner": "ssh"}]})}]}


# ── signals ──────────────────────────────────────────────────────────────────────────
def test_gather_signals_counts_real_evidence(monkeypatch):
    from friday.core import dreaming, playbook, train

    monkeypatch.setattr(train, "load_traces", lambda: [_port_scan_trace("a"),
                                                       {"goal": "b", "report": "", "messages": []}])
    monkeypatch.setattr(dreaming, "_load", lambda: [{"lesson": "enumerate harder"}])
    monkeypatch.setattr(playbook, "principles", lambda limit=0: ["p1", "p2", "p3"])

    sig = study.gather_signals()
    assert sig.n_traces == 2 and sig.n_successful == 1     # only the port_scan trace found a thing
    assert sig.lessons == ["enumerate harder"]
    assert sig.playbook_size == 3 and sig.has_material


def test_signals_empty_has_no_material(monkeypatch):
    from friday.core import dreaming, train

    monkeypatch.setattr(train, "load_traces", lambda: [])
    monkeypatch.setattr(dreaming, "_load", lambda: [])
    assert study.gather_signals().has_material is False


# ── reflection parsing ───────────────────────────────────────────────────────────────
def test_parse_reflection_tolerates_prose_and_garbage():
    assert study._parse_reflection('here you go {"focus": "smb"} cheers')["focus"] == "smb"
    assert study._parse_reflection("no json here") == {}
    assert study._parse_reflection('{"principles": [broken') == {}


async def test_reflect_is_free_only_and_caps_principles(monkeypatch):
    captured = {}

    async def _complete(messages, tier=None, tools=None, free_only=False, **kw):
        captured["free_only"] = free_only
        return _resp(json.dumps({"principles": ["one", "two", "three", "four"],
                                 "hygiene": ["gobuster timed out"], "focus": "try SMB"}))

    monkeypatch.setattr(study, "complete", _complete)
    refl = await study.reflect(study.Signals(n_traces=3), tier=Tier.MEDIUM)
    assert captured["free_only"] is True                      # never spends the paid budget
    assert refl.principles == ["one", "two"]                  # capped at _MAX_NEW_PRINCIPLES
    assert refl.hygiene == ["gobuster timed out"] and refl.focus == "try SMB"


async def test_reflect_empty_on_unparseable(monkeypatch):
    async def _complete(*a, **k):
        return _resp("I could not produce JSON")

    monkeypatch.setattr(study, "complete", _complete)
    refl = await study.reflect(study.Signals(n_traces=1))
    assert refl.principles == [] and refl.focus == ""        # records nothing rather than fabricate


# ── the full pass ────────────────────────────────────────────────────────────────────
async def test_run_study_no_material_writes_honest_digest(monkeypatch):
    monkeypatch.setattr(study, "gather_signals", lambda: study.Signals())

    async def _boom(*a, **k):
        raise AssertionError("must not call the model with no data to learn from")

    monkeypatch.setattr(study, "complete", _boom)
    path, refl, status = await study.run_study()
    assert refl is None and status == "no-data"
    assert "No engagement data yet" in path.read_text()


async def test_run_study_records_principles_unless_dry_run(monkeypatch, tmp_path):
    from friday.core import playbook

    monkeypatch.setattr(playbook, "_STORE", tmp_path / "playbook.jsonl")
    monkeypatch.setattr(study, "gather_signals",
                        lambda: study.Signals(n_traces=2, n_successful=1, lessons=["x"]))

    async def _reflect(signals, *, tier=Tier.MEDIUM):
        return study.Reflection(principles=["Snapshot creds the moment you find them."],
                                hygiene=[], focus="smb")

    monkeypatch.setattr(study, "reflect", _reflect)

    path, refl, status = await study.run_study(write=True)
    assert status == "reflected" and "Snapshot creds" in path.read_text()
    assert any("Snapshot creds" in p for p in playbook.principles(limit=0))  # grew the doctrine

    # propose-only: same reflection (force past the change-guard), playbook must NOT change
    before = playbook.principles(limit=0)
    path2, _, _ = await study.run_study(write=False, force=True)
    assert playbook.principles(limit=0) == before
    assert "not applied" in path2.read_text().lower()


async def test_run_study_skips_when_nothing_new(monkeypatch, tmp_path):
    # the 24/7 guard: once it has reflected on a state, an unchanged re-run must NOT call the model
    monkeypatch.setattr(study, "gather_signals",
                        lambda: study.Signals(n_traces=1, n_successful=1, lessons=["x"]))
    calls = {"n": 0}

    async def _reflect(signals, *, tier=Tier.MEDIUM):
        calls["n"] += 1
        return study.Reflection(principles=[], focus="")

    monkeypatch.setattr(study, "reflect", _reflect)

    _, _, s1 = await study.run_study(write=False)
    _, _, s2 = await study.run_study(write=False)            # nothing changed → skip
    assert s1 == "reflected" and s2 == "unchanged"
    assert calls["n"] == 1                                    # the model ran only once

    _, _, s3 = await study.run_study(write=False, force=True)  # --force overrides the guard
    assert s3 == "reflected" and calls["n"] == 2


# ── CLI + scheduler ──────────────────────────────────────────────────────────────────
def test_cli_study_proposes_by_default_and_applies_on_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("COLUMNS", "200")  # keep Rich from wrapping the long tmp path mid-token
    seen = {}

    async def _run_study(*, write=True, tier=None, force=False):
        seen["write"], seen["force"] = write, force
        return tmp_path / "2026-06-23.md", study.Reflection(principles=["learned a thing"]), \
            "reflected"

    monkeypatch.setattr(study, "run_study", _run_study)

    r = runner.invoke(cli.app, ["study"])           # default: propose only
    assert r.exit_code == 0, r.output
    assert seen["write"] is False and seen["force"] is False and "Proposed" in r.output
    assert "learned a thing" in r.output and "2026-06-23.md" in r.output

    r2 = runner.invoke(cli.app, ["study", "--apply", "--force"])  # opt in + override the guard
    assert r2.exit_code == 0 and seen["write"] is True and seen["force"] is True
    assert "Added to the playbook" in r2.output


def test_cli_install_timer_writes_units(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))  # Path.home() reads $HOME on posix
    r = runner.invoke(cli.app, ["study", "--install-timer"])
    assert r.exit_code == 0, r.output
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    assert (unit_dir / "sutra-study.service").exists()
    timer = (unit_dir / "sutra-study.timer").read_text()
    assert "OnUnitActiveSec=30min" in timer and "OnBootSec" in timer  # continuous while on
