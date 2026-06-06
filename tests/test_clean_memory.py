"""Tests for the near-duplicate detector in scripts/clean_memory.py.

Pure logic only (token sets, Jaccard, clustering) — no FalkorDB needed.
"""

from scripts.clean_memory import _dup_clusters, _similar, _tokens


def _ep(uuid, content):
    return {"uuid": uuid, "name": f"n-{uuid}", "content": content}


def test_tokens_normalize_case_and_punctuation():
    assert _tokens("Sarjan is from Sudbury, Ontario!") == _tokens("sarjan is from sudbury ontario")


def test_similar_identical_and_disjoint():
    a = _tokens("the quick brown fox")
    assert _similar(a, a) == 1.0
    assert _similar(_tokens("alpha beta"), _tokens("gamma delta")) == 0.0
    assert _similar(frozenset(), _tokens("x")) == 0.0  # empty never matches


def test_dup_clusters_groups_exact_copies_keeps_first():
    eps = [
        _ep("a", "Sarjan is building Sutra, a voice assistant."),
        _ep("b", "Sarjan is building Sutra, a voice assistant."),
        _ep("c", "Something entirely unrelated about the weather."),
        _ep("d", "Sarjan is building Sutra, a voice assistant."),
    ]
    clusters = _dup_clusters(eps, threshold=0.9)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert [e["uuid"] for e in cluster] == ["a", "b", "d"]  # order preserved, first kept


def test_dup_clusters_threshold_separates_near_misses():
    eps = [
        _ep("a", "Sarjan spent about nine minutes in Terminal reading the handoff doc."),
        _ep("b", "Sarjan spent about three minutes in Firefox browsing a profile page."),
    ]
    # Different enough that a strict cutoff keeps them apart (no cluster).
    assert _dup_clusters(eps, threshold=0.9) == []


def test_dup_clusters_ignores_singletons():
    eps = [_ep("a", "unique one"), _ep("b", "unique two")]
    assert _dup_clusters(eps, threshold=0.5) == []
