"""Tests for Dreaming — the lessons log that lets engagements learn from past ones."""

import numpy as np

from friday.core import dreaming
from friday.rag import store as rag_store


def test_record_and_load_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(dreaming, "_LESSONS", tmp_path / "lessons.jsonl")
    dreaming.record_lesson("pwn 10.10.10.5", "smb was the foothold; check the vsftpd backdoor")
    rows = dreaming._load()
    assert len(rows) == 1 and rows[0]["topic"] == "pwn 10.10.10.5"
    assert "vsftpd" in rows[0]["lesson"] and "ts" in rows[0]


def test_record_skips_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(dreaming, "_LESSONS", tmp_path / "lessons.jsonl")
    dreaming.record_lesson("topic", "   ")
    assert dreaming._load() == []


def test_recall_ranks_by_relevance(monkeypatch, tmp_path):
    monkeypatch.setattr(dreaming, "_LESSONS", tmp_path / "lessons.jsonl")
    dreaming.record_lesson("web box", "sqli in the login form")
    dreaming.record_lesson("ad box", "kerberoasting got the admin hash")

    def _fake_embed(texts, kind="document"):
        def v(t):
            t = t.lower()
            return np.array([
                1.0 if ("sql" in t or "web" in t or "login" in t) else 0.0,
                1.0 if ("kerber" in t or " ad " in f" {t} " or "hash" in t) else 0.0,
                0.1,
            ], dtype=np.float32)
        m = np.array([v(t) for t in texts], dtype=np.float32)
        return m / np.clip(np.linalg.norm(m, axis=1, keepdims=True), 1e-8, None)

    monkeypatch.setattr(rag_store, "embed", _fake_embed)
    top = dreaming.recall_lessons("how do I attack the web login", k=1)
    assert len(top) == 1 and "sqli" in top[0]["lesson"]


def test_recall_falls_back_to_recent_when_embed_down(monkeypatch, tmp_path):
    monkeypatch.setattr(dreaming, "_LESSONS", tmp_path / "lessons.jsonl")
    dreaming.record_lesson("a", "first")
    dreaming.record_lesson("b", "second")

    def _boom(texts, kind="document"):
        raise rag_store.EmbedUnavailable("ollama down")

    monkeypatch.setattr(rag_store, "embed", _boom)
    out = dreaming.recall_lessons("anything", k=1)
    assert len(out) == 1 and out[0]["lesson"] == "second"  # degrades to the most recent


def test_lessons_block_empty_then_formatted(monkeypatch, tmp_path):
    monkeypatch.setattr(dreaming, "_LESSONS", tmp_path / "lessons.jsonl")
    assert dreaming.lessons_block("x") == ""  # nothing recorded yet
    dreaming.record_lesson("topic1", "do the thing")
    monkeypatch.setattr(dreaming, "recall_lessons", lambda q, k=3: dreaming._load())
    block = dreaming.lessons_block("x")
    assert "Lessons from past engagements" in block and "do the thing" in block


def test_record_lesson_caps_growth(monkeypatch, tmp_path):
    # Long-term: the lessons log must stay bounded (which also bounds recall's embed cost).
    monkeypatch.setattr(dreaming, "_LESSONS", tmp_path / "lessons.jsonl")
    monkeypatch.setattr(dreaming, "_MAX_LESSONS", 5)
    for i in range(9):
        dreaming.record_lesson(f"box{i}", f"lesson {i}")
    rows = dreaming._load()
    assert len(rows) == 5                        # capped, not 9
    assert rows[-1]["lesson"] == "lesson 8"      # keeps the most recent
    assert rows[0]["lesson"] == "lesson 4"


def test_append_capped_keeps_last_n(tmp_path):
    from friday.core.turnlog import append_capped

    p = tmp_path / "log.jsonl"
    for i in range(10):
        append_capped(p, f"line{i}", max_lines=3)
    assert p.read_text().splitlines() == ["line7", "line8", "line9"]  # bounded to the last 3
