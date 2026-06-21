"""Skill-level tracking — the tutor learns what Sarjan knows and adapts its depth.

Covers the persistent profile (friday/core/skill_profile.py), the per-turn classification, and
the injection into agent.tutor.
"""

from __future__ import annotations

import types

import pytest

from friday.core import agent, skill_profile


@pytest.fixture
def profile_path(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_profile, "_PROFILE", tmp_path / "skill_profile.json")
    return skill_profile._PROFILE


def _resp(content):
    msg = types.SimpleNamespace(content=content)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


# --- storage ------------------------------------------------------------------------

def test_record_and_summarize(profile_path):
    assert skill_profile.summarize() == ""                  # empty on a fresh install
    skill_profile.record("nmap enumeration", "comfortable", "used -sV")
    skill_profile.record("linux privesc", "new")
    out = skill_profile.summarize()
    assert "nmap enumeration: comfortable" in out and "used -sV" in out
    assert "linux privesc: new" in out


def test_level_ratchets_up_not_down(profile_path):
    skill_profile.record("gobuster", "strong")
    skill_profile.record("gobuster", "learning")            # a stumble must not erase mastery
    entry = skill_profile._load()["gobuster"]
    assert entry["level"] == "strong" and entry["seen"] == 2


def test_record_ignores_blank_topic(profile_path):
    skill_profile.record("", "strong")
    assert skill_profile._load() == {}


def test_unknown_level_defaults_to_learning(profile_path):
    skill_profile.record("xss", "guru")                     # not a real rung
    assert skill_profile._load()["xss"]["level"] == "learning"


def test_clear_wipes_profile(profile_path):
    skill_profile.record("sqli", "learning")
    skill_profile.clear()
    assert skill_profile.summarize() == ""


# --- classification (agent) ---------------------------------------------------------

async def test_classify_skill_records_from_model(profile_path, monkeypatch):
    async def fake_complete(messages, **kw):
        return _resp('{"topic": "sql injection", "level": "learning", "note": "asked re union"}')

    monkeypatch.setattr(agent, "complete", fake_complete)
    await agent._classify_skill("how does union sqli work?", "Here's the idea…", agent.Tier.MEDIUM)
    assert skill_profile._load()["sql injection"]["level"] == "learning"


async def test_classify_skill_strips_code_fence(profile_path, monkeypatch):
    async def fake_complete(messages, **kw):
        return _resp('```json\n{"topic": "nmap", "level": "strong", "note": ""}\n```')

    monkeypatch.setattr(agent, "complete", fake_complete)
    await agent._classify_skill("I always start with nmap -sC -sV", "Good.", agent.Tier.MEDIUM)
    assert skill_profile._load()["nmap"]["level"] == "strong"


async def test_classify_skill_best_effort_on_garbage(profile_path, monkeypatch):
    async def fake_complete(messages, **kw):
        return _resp("not json at all")

    monkeypatch.setattr(agent, "complete", fake_complete)
    await agent._classify_skill("q", "a", agent.Tier.MEDIUM)   # must not raise
    assert skill_profile._load() == {}                          # nothing recorded


# --- injection into the tutor -------------------------------------------------------

async def test_tutor_injects_skill_profile(profile_path, monkeypatch):
    skill_profile.record("web enumeration", "strong", "knows robots.txt")
    seen = {}

    async def fake_complete(messages, tier=None, tools=None, **kw):
        seen["system"] = messages[0]["content"]
        return _resp("ok")

    monkeypatch.setattr(agent, "complete", fake_complete)
    monkeypatch.setattr("friday.core.dreaming.lessons_block", lambda *a, **k: "")
    await agent.tutor("what next on this web box?")
    assert "web enumeration: strong" in seen["system"]          # adapts to what he already knows


def test_update_skill_profile_async_no_loop_is_safe():
    # No running loop (sync caller) → must no-op, not raise.
    agent.update_skill_profile_async("topic", "reply")
