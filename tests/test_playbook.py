"""Tests for the operating playbook — Sutra's working doctrine, distilled + growable."""

import json
import types

import pytest

from friday.core import playbook


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch, tmp_path):
    """Keep every test off the real learned-playbook log."""
    monkeypatch.setattr(playbook, "_STORE", tmp_path / "playbook.jsonl")


def test_block_carries_the_core_doctrine():
    block = playbook.playbook_block()
    assert "working doctrine" in block.lower()
    # a couple of the load-bearing principles must be present
    assert "Ground every claim in real tool output" in block
    assert "cheapest path" in block
    assert block.startswith("\n\n")  # stacks cleanly after the system prompt


def test_record_principle_adds_and_surfaces():
    assert playbook.record_principle("Always re-read the box notes before the next objective.")
    assert "re-read the box notes" in playbook.playbook_block(limit=0)
    # and it was persisted as one jsonl row with provenance
    rows = [json.loads(s) for s in playbook._STORE.read_text().splitlines() if s.strip()]
    assert len(rows) == 1 and rows[0]["source"] == "learned"


def test_record_principle_dedups_core_and_learned():
    # a restatement of a core principle (case/space-insensitive) is rejected — derived from the
    # constant so the test can't drift from the real wording
    core = playbook._CORE_PRINCIPLES[1]
    assert playbook.record_principle("   " + core.upper() + "   ") is False
    # first novel add succeeds, the same thing again is a no-op
    assert playbook.record_principle("Snapshot creds the moment you find them.") is True
    assert playbook.record_principle("snapshot creds the MOMENT you find them.") is False
    assert not playbook._STORE.exists() or \
        len([s for s in playbook._STORE.read_text().splitlines() if s.strip()]) == 1


def test_record_principle_rejects_blank():
    assert playbook.record_principle("   ") is False
    assert playbook.record_principle("") is False


def test_core_always_present_and_learned_is_most_recent_first(monkeypatch):
    # core is never crowded out; learned principles surface newest-first after it
    playbook.record_principle("OLDER learned principle.")
    playbook.record_principle("NEWER learned principle.")
    full = playbook.principles(limit=0)
    assert full[: len(playbook._CORE_PRINCIPLES)] == list(playbook._CORE_PRINCIPLES)
    learned = full[len(playbook._CORE_PRINCIPLES):]
    assert learned.index("NEWER learned principle.") < learned.index("OLDER learned principle.")
    # a tight limit keeps the whole core and simply shows fewer learned ones
    capped = playbook.principles(limit=len(playbook._CORE_PRINCIPLES))
    assert capped == list(playbook._CORE_PRINCIPLES)


def test_corrupt_store_degrades_to_core(monkeypatch):
    playbook._STORE.write_text("not json\n{also bad\n")
    assert playbook._load_learned() == []          # never raises
    assert "working doctrine" in playbook.playbook_block().lower()  # core still renders


# ── the point of the feature: the doctrine is injected into the real autonomous prompts ──

class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class _Resp:
    def __init__(self, msg):
        self.choices = [types.SimpleNamespace(message=msg)]


async def test_doctrine_is_injected_into_run_autonomous(monkeypatch, tmp_path):
    from friday.core import agent, dreaming

    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)
    monkeypatch.setattr(dreaming, "recall_lessons", lambda *a, **k: [])
    monkeypatch.setattr(dreaming, "record_lesson", lambda *a, **k: None)
    seen = {}

    async def _complete(messages, tier=None, tools=None, **kw):
        seen["system"] = messages[0]["content"]
        return _Resp(_Msg(content="done — nothing to do"))

    monkeypatch.setattr(agent, "complete", _complete)
    await agent.run_autonomous("scan it", max_rounds=2)
    assert "working doctrine" in seen["system"].lower()
    assert "Decompose before you act" in seen["system"]  # a core principle reached the model
