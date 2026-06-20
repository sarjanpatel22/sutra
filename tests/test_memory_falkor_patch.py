"""The FalkorDB fulltext-sanitizer backtick patch (friday/memory/store.py).

Graphiti 0.29.1's FalkorDB sanitizer omits the backtick, so a remembered fact with a markdown
code span leaked a bare ` into the RediSearch query and raised a syntax error inside add_episode
— losing the whole episode. These verify our monkeypatch closes that without a live FalkorDB.
"""

from __future__ import annotations

import pytest

from friday.memory import store


def test_patch_adds_backtick_to_falkor_separator_map():
    search_ops = pytest.importorskip("graphiti_core.driver.falkordb.operations.search_ops")
    # start from a map WITHOUT the backtick so we exercise the patch itself
    search_ops._SEPARATOR_MAP = {k: v for k, v in search_ops._SEPARATOR_MAP.items()
                                 if k != ord("`")}
    store._falkor_sanitizer_patched = False

    store._patch_falkor_fulltext_sanitizer()

    assert ord("`") in search_ops._SEPARATOR_MAP


def test_patched_query_builder_strips_backticks():
    search_ops = pytest.importorskip("graphiti_core.driver.falkordb.operations.search_ops")
    store._falkor_sanitizer_patched = False
    store._patch_falkor_fulltext_sanitizer()

    # the exact shape that broke live: a markdown code span in the remembered text
    q = search_ops._build_falkor_fulltext_query(
        "User wrote `~/thm_kit/rooms.md` with the rooms", group_ids=["sarjan"]
    )
    assert "`" not in q              # no bare backtick → no RediSearch syntax error
    assert "(@group_id:" in q        # still a well-formed fulltext query


def test_patch_is_idempotent():
    store._falkor_sanitizer_patched = False
    store._patch_falkor_fulltext_sanitizer()
    store._patch_falkor_fulltext_sanitizer()  # a second call must not raise or double-apply
    assert store._falkor_sanitizer_patched is True
