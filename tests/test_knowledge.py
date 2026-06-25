"""Tests for fast knowledge learning (friday/core/knowledge.py) — mocked ingest, hermetic."""

from friday.core import knowledge


def test_learn_knowledge_ingests_the_knowledge_dir(monkeypatch):
    seen: dict = {}

    def fake_ingest(paths, collection):
        seen["paths"] = paths
        seen["collection"] = collection
        return {"chunks": 7, "total": 40, "files": 5}

    monkeypatch.setattr("friday.rag.ingest.ingest_paths", fake_ingest)
    res = knowledge.learn_knowledge()

    assert seen["collection"] == "bugbounty"                       # the coaching corpus
    assert seen["paths"][0].endswith("knowledge")                  # the docs/knowledge dir
    assert res == {"learned": 7, "total": 40, "files": 5}          # learned = newly added chunks


def test_learn_knowledge_handles_missing_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(knowledge, "KNOWLEDGE_DIR", tmp_path / "nope")
    res = knowledge.learn_knowledge()
    assert res["learned"] == 0 and "error" in res                  # graceful, no crash
