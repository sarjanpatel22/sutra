"""Tests for `sutra tutor` — pentest-mentor mode (teaches the why, no tools, $0 default)."""

from friday.core import agent


class _Msg:
    def __init__(self, content):
        self.content = content


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": _Msg(content)})()]


async def test_tutor_teaches_with_no_tools(monkeypatch):
    seen = {}

    async def _fake_complete(messages, tier=None, tools=None, **kw):
        seen["system"] = messages[0]["content"]
        seen["tools"] = tools
        return _Resp("Approach: enumerate. Try: nmap -sC -sV. Concept: service discovery.")

    monkeypatch.setattr(agent, "complete", _fake_complete)
    monkeypatch.setattr("friday.core.dreaming.lessons_block", lambda *a, **k: "")
    out = await agent.tutor("how do I approach an SMB box")
    assert "Approach" in out
    assert seen["tools"] is None  # teaching, never acts
    assert "methodolog" in seen["system"].lower() or "mentor" in seen["system"].lower()


async def test_tutor_injects_dreaming_lessons(monkeypatch):
    seen = {}

    async def _fake_complete(messages, tier=None, tools=None, **kw):
        seen["system"] = messages[0]["content"]
        return _Resp("ok")

    monkeypatch.setattr(agent, "complete", _fake_complete)
    monkeypatch.setattr(
        "friday.core.dreaming.lessons_block",
        lambda topic, k=3: "\n\nLessons: smb null session worked",
    )
    await agent.tutor("smb box")
    assert "smb null session worked" in seen["system"]  # teaches from real past experience


async def test_tutor_empty_topic_guides():
    out = await agent.tutor("   ")
    assert "learn" in out.lower() or "stuck" in out.lower()


async def test_tutor_grounds_in_screen(monkeypatch):
    seen = {}

    async def _fake(messages, tier=None, tools=None, **kw):
        seen["user"] = messages[-1]["content"]
        return _Resp("ok")

    monkeypatch.setattr(agent, "complete", _fake)
    monkeypatch.setattr("friday.core.dreaming.lessons_block", lambda *a, **k: "")
    await agent.tutor("what next?", screen="80/tcp open http")
    assert "80/tcp open http" in seen["user"] and "what next?" in seen["user"]


async def test_tutor_threads_history(monkeypatch):
    seen = {}

    async def _fake(messages, tier=None, tools=None, **kw):
        seen["roles"] = [m["role"] for m in messages]
        return _Resp("ok")

    monkeypatch.setattr(agent, "complete", _fake)
    monkeypatch.setattr("friday.core.dreaming.lessons_block", lambda *a, **k: "")
    hist = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]
    await agent.tutor("now", history=hist)
    assert seen["roles"] == ["system", "user", "assistant", "user"]  # system + history + current


async def test_tutor_reveals_only_on_explicit_request(monkeypatch):
    seen = {}

    async def _fake(messages, tier=None, tools=None, **kw):
        seen["system"] = messages[0]["content"]
        return _Resp("ok")

    monkeypatch.setattr(agent, "complete", _fake)
    monkeypatch.setattr("friday.core.dreaming.lessons_block", lambda *a, **k: "")
    await agent.tutor("what should I try next?")
    assert agent._TUTOR_REVEAL not in seen["system"]          # Socratic — no spoiler by default
    await agent.tutor("just tell me the command")
    assert agent._TUTOR_REVEAL in seen["system"]              # relents on explicit ask


# --- hint-learning loop: did the hint land? -----------------------------------------

def _capture_lessons(monkeypatch):
    recorded: list[tuple] = []
    monkeypatch.setattr(
        "friday.core.dreaming.record_lesson",
        lambda topic, lesson, tags=(): recorded.append((topic, lesson, tags)),
    )
    return recorded


def test_hint_outcome_records_when_it_lands(monkeypatch):
    recorded = _capture_lessons(monkeypatch)
    agent.note_hint_outcome("stuck on port 80", "try gobuster for hidden dirs", "oh that worked!")
    assert recorded and "worked" in recorded[0][2]            # tagged as a win
    assert "gobuster" in recorded[0][1]                       # records the hint that worked


def test_hint_outcome_records_a_miss(monkeypatch):
    recorded = _capture_lessons(monkeypatch)
    agent.note_hint_outcome("privesc", "check sudo -l", "still stuck, that didn't work")
    assert recorded and "missed" in recorded[0][2]


def test_hint_outcome_ignores_neutral_feedback(monkeypatch):
    recorded = _capture_lessons(monkeypatch)
    agent.note_hint_outcome("topic", "a hint", "ok, what about the other port?")
    assert recorded == []                                     # no explicit signal → learn nothing


def test_hint_outcome_from_history_extracts_last_exchange(monkeypatch):
    recorded = _capture_lessons(monkeypatch)
    history = [
        {"role": "user", "content": "how do I enumerate the web server"},
        {"role": "assistant", "content": "run gobuster against port 80"},
    ]
    agent.note_hint_outcome_from_history(history, "got it, that worked!")
    assert recorded
    assert "gobuster" in recorded[0][1]                       # the hint
    assert "enumerate the web server" in recorded[0][0]       # the situation it addressed


def test_hint_outcome_from_history_empty_is_safe(monkeypatch):
    recorded = _capture_lessons(monkeypatch)
    agent.note_hint_outcome_from_history([], "that worked")
    agent.note_hint_outcome_from_history(None, "that worked")
    assert recorded == []


async def test_tutor_coaches_from_engagement_context(monkeypatch):
    seen = {}

    async def _fake(messages, tier=None, tools=None, **kw):
        seen["system"] = messages[0]["content"]
        return _Resp("ok")

    monkeypatch.setattr(agent, "complete", _fake)
    monkeypatch.setattr("friday.core.dreaming.lessons_block", lambda *a, **k: "")
    await agent.tutor("what's my next move?",
                      engagement="## Findings\n- Open ports on 10.10.10.5: 22, 80\n- /admin [301]")
    assert "10.10.10.5" in seen["system"] and "/admin" in seen["system"]
    assert "CURRENT BOX" in seen["system"]  # framed so the model coaches from real findings


def test_latest_engagement_findings_reads_newest_head(monkeypatch, tmp_path):
    import os

    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path)
    assert agent._latest_engagement_findings() is None  # nothing yet
    (tmp_path / "old.md").write_text("# Engagement: old\n## Findings\n- old\n## Transcript\nx")
    (tmp_path / "new.md").write_text(
        "# Engagement: new box\n## Flags\n- THM{x}\n## Findings\n- port 22\n## Transcript\nnoise")
    os.utime(tmp_path / "old.md", (1000, 1000))
    os.utime(tmp_path / "new.md", (2000, 2000))  # newest regardless of FS timing
    out = agent._latest_engagement_findings()
    assert "new box" in out and "THM{x}" in out and "port 22" in out
    assert "noise" not in out and "old" not in out  # newest only, transcript dropped
