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
