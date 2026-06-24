"""Tests for `sutra learning` — the monitor over what Sutra is learning / has learned."""

from typer.testing import CliRunner

import friday.cli as cli
from friday.core import learning

runner = CliRunner()


def test_digest_headline_extracts_learned(tmp_path):
    d = tmp_path / "x.md"
    d.write_text("# t\n## Reviewed\n- a\n## Learned — new principles\n- Principle one\n"
                 "- Principle two\n## Skill hygiene\n- z\n")
    assert learning._digest_headline(d) == "Principle one · Principle two"

    empty = tmp_path / "e.md"
    empty.write_text("# t\n## Learned\nNo engagement data yet — run boxes\n")
    assert "No engagement data" in learning._digest_headline(empty)


def test_list_collections(monkeypatch, tmp_path):
    (tmp_path / "bugbounty").mkdir()
    (tmp_path / "default").mkdir()
    (tmp_path / "notadir.txt").write_text("x")
    monkeypatch.setattr("friday.rag.store._RAG_DIR", tmp_path)
    monkeypatch.setattr("friday.rag.store.RagStore",
                        lambda name: type("S", (), {"__len__": lambda self: 7})())

    cols = dict(learning._list_collections())
    assert cols == {"bugbounty": 7, "default": 7}   # the stray file is skipped


def test_snapshot_aggregates_every_surface(monkeypatch, tmp_path):
    from friday.core import dreaming, playbook, study

    d = tmp_path / "2026-06-23.md"
    d.write_text("# Sutra study\n## Learned — new principles (proposed)\n- Always snapshot creds\n"
                 "## Skill hygiene\n- x\n")
    _lesson = {"topic": "smb", "lesson": "null session worked"}
    monkeypatch.setattr(study, "latest_digests", lambda n=5: [d])
    monkeypatch.setattr(dreaming, "_load", lambda: [_lesson])
    monkeypatch.setattr(playbook, "_load_learned", lambda: ["Earned principle A"])
    monkeypatch.setattr(learning, "_timer_active", lambda: True)
    monkeypatch.setattr(learning, "_list_collections", lambda: [("bugbounty", 8)])

    snap = learning.snapshot()
    assert snap.timer_active is True
    assert snap.doctrine == ["Earned principle A"]
    assert snap.digests[0][0] == "2026-06-23" and "snapshot creds" in snap.digests[0][1]
    assert snap.lessons == [("smb", "null session worked")]
    assert snap.collections == [("bugbounty", 8)]


def test_snapshot_empty_is_graceful(monkeypatch):
    from friday.core import dreaming, playbook, study

    monkeypatch.setattr(study, "latest_digests", lambda n=5: [])
    monkeypatch.setattr(dreaming, "_load", lambda: [])
    monkeypatch.setattr(playbook, "_load_learned", lambda: [])
    monkeypatch.setattr(learning, "_timer_active", lambda: False)
    monkeypatch.setattr(learning, "_list_collections", lambda: [])

    snap = learning.snapshot()
    assert snap.last_study == "never" and snap.doctrine == [] and snap.digests == []


def test_cli_learning_renders(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(learning, "snapshot", lambda: learning.LearningSnapshot(
        timer_active=True, last_study="2026-06-23 23:00", doctrine=["Earned P1"],
        digests=[("2026-06-23", "learned a thing")], lessons=[("smb", "null session")],
        collections=[("bugbounty", 8)]))
    r = runner.invoke(cli.app, ["learning"])
    assert r.exit_code == 0, r.output
    assert "what it's learning" in r.output
    assert "Earned P1" in r.output and "bugbounty" in r.output and "learned a thing" in r.output
