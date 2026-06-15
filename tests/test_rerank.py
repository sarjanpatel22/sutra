"""Reranker tests — ordering logic, fail-safe fallback, and recall-index wiring.

The reranker can only ever sharpen recall: any failure must degrade to the
bi-encoder's input order, and it must never drop a candidate. These pin that
contract without touching the network (litellm.completion is stubbed).
"""

from __future__ import annotations

import types

import pytest

from friday.memory.rerank import rerank, rerank_enabled


def _resp(content: str) -> types.SimpleNamespace:
    """Fake litellm.completion return: r.choices[0].message.content."""
    msg = types.SimpleNamespace(content=content)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


# --- rerank_enabled env parsing ------------------------------------------------

@pytest.mark.parametrize(
    "val,want",
    [("1", True), ("true", True), ("YES", True), ("on", True),
     ("0", False), ("false", False), ("no", False), ("", False)],
)
def test_rerank_enabled_parsing(monkeypatch, val, want):
    monkeypatch.setenv("RECALL_RERANK", val)
    assert rerank_enabled() is want


def test_rerank_disabled_when_unset(monkeypatch):
    monkeypatch.delenv("RECALL_RERANK", raising=False)
    assert rerank_enabled() is False


# --- trivial cases never call the model ---------------------------------------

def test_single_candidate_short_circuits(monkeypatch):
    import litellm
    # Guard fires before the model call — patch completion to prove it's never reached.
    monkeypatch.setattr(litellm, "completion",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("should not call")))
    assert rerank("who?", ["only one"], k=5) == ["only one"]


def test_empty_query_short_circuits():
    assert rerank("   ", ["a", "b"], k=1) == ["a"]


# --- ordering -----------------------------------------------------------------

def test_reorders_by_model_output(monkeypatch):
    import litellm
    monkeypatch.setattr(litellm, "completion", lambda **kw: _resp("2,0,3,1"))
    assert rerank("q", ["A", "B", "C", "D"], k=4) == ["C", "A", "D", "B"]


def test_truncates_to_k(monkeypatch):
    import litellm
    monkeypatch.setattr(litellm, "completion", lambda **kw: _resp("3,2,1,0"))
    assert rerank("q", ["A", "B", "C", "D"], k=2) == ["D", "C"]


def test_appends_unmentioned_never_drops(monkeypatch):
    """Model names only index 2 — the rest must still follow, in original order."""
    import litellm
    monkeypatch.setattr(litellm, "completion", lambda **kw: _resp("2"))
    assert rerank("q", ["A", "B", "C"], k=5) == ["C", "A", "B"]


def test_ignores_out_of_range_and_duplicate_indices(monkeypatch):
    import litellm
    monkeypatch.setattr(litellm, "completion", lambda **kw: _resp("9,1,1,0"))
    assert rerank("q", ["A", "B"], k=2) == ["B", "A"]


def test_falls_back_to_input_order_on_error(monkeypatch):
    import litellm

    def boom(**kw):
        raise RuntimeError("rate limit")

    monkeypatch.setattr(litellm, "completion", boom)
    assert rerank("q", ["A", "B", "C"], k=2) == ["A", "B"]


def test_falls_back_on_garbage_reply(monkeypatch):
    """No parseable indices → keep the bi-encoder order (nothing matched, all appended)."""
    import litellm
    monkeypatch.setattr(litellm, "completion", lambda **kw: _resp("sorry, no idea"))
    assert rerank("q", ["A", "B", "C"], k=3) == ["A", "B", "C"]


# --- recall_index wiring ------------------------------------------------------

def test_recall_facts_skips_rerank_when_disabled(monkeypatch):
    from friday.memory import recall_index as ri

    monkeypatch.setenv("RECALL_RERANK", "0")
    monkeypatch.setattr(ri, "_topn_scored",
                        lambda q, n: [("f1", 0.9), ("f2", 0.8), ("f3", 0.7)])
    assert ri.recall_facts("q", k=2) == ["f1", "f2"]


def test_recall_facts_reranks_when_enabled(monkeypatch):
    import friday.memory.rerank as rr
    from friday.memory import recall_index as ri

    monkeypatch.setenv("RECALL_RERANK", "1")
    monkeypatch.setattr(ri, "_topn_scored",
                        lambda q, n: [("f1", 0.9), ("f2", 0.8), ("f3", 0.7)])
    monkeypatch.setattr(rr, "rerank", lambda q, c, k: list(reversed(c))[:k])
    assert ri.recall_facts("q", k=2) == ["f3", "f2"]


def test_recall_facts_empty_when_no_candidates(monkeypatch):
    from friday.memory import recall_index as ri

    monkeypatch.setenv("RECALL_RERANK", "1")
    monkeypatch.setattr(ri, "_topn_scored", lambda q, n: [])
    assert ri.recall_facts("q", k=5) == []


def test_recall_scored_keeps_cosine_scores_after_rerank(monkeypatch):
    import friday.memory.rerank as rr
    from friday.memory import recall_index as ri

    monkeypatch.setenv("RECALL_RERANK", "1")
    monkeypatch.setattr(ri, "_topn_scored",
                        lambda q, n: [("f1", 0.9), ("f2", 0.8), ("f3", 0.7)])
    monkeypatch.setattr(rr, "rerank", lambda q, c, k: ["f3", "f1"][:k])
    # LLM sets the order; each fact keeps its own cosine score for display.
    assert ri.recall_scored("q", k=2) == [("f3", 0.7), ("f1", 0.9)]
