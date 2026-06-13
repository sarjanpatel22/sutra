"""Tests for the screen-viewer agent (web_task_vision) — mocked, no real browser/LLM."""

import friday.skills.vision_agent as va
from friday.skills import registry
from friday.skills.vision_agent import _parse, web_task_vision


def test_parse_action_and_garbage():
    assert _parse('{"act":"click","n":3}') == {"act": "click", "n": 3}
    assert _parse('sure: {"act":"done","answer":"hi"} ok') == {"act": "done", "answer": "hi"}
    assert _parse("no json") is None
    assert _parse('{"foo":1}') is None  # valid JSON but no "act"


def test_web_task_vision_is_gated():
    registry.autodiscover()
    assert registry.get("web_task_vision").auto_confirm is False


async def test_requires_goal_and_http():
    assert "goal" in (await web_task_vision("", "https://x.com"))["error"]
    assert "http" in (await web_task_vision("do it", "file:///etc/passwd"))["error"].lower()


class _Handle:
    def __init__(self, log):
        self._log = log

    async def click(self, **k):
        self._log.append("click")

    async def fill(self, t):
        self._log.append(f"fill:{t}")

    async def press(self, k):
        self._log.append(f"press:{k}")


class _Page:
    url = "https://x.test/"

    def __init__(self, log):
        self._log = log
        self.mouse = self

    async def goto(self, url, **k):
        type(self).url = url

    async def wait_for_load_state(self, *a, **k):
        return None

    async def evaluate(self, js, *a):
        return [{"idx": 0, "tag": "input", "label": "Search"}] if a else None  # _MARK / _CLEAR

    async def screenshot(self, *a, **k):
        return b"\x89PNG-fake"

    async def query_selector(self, _sel):
        return _Handle(self._log)

    async def wheel(self, *a, **k):
        self._log.append("scroll")


def _llm(*replies):
    seq = iter(replies)

    async def fake(model, messages, **k):
        msg = type("M", (), {"content": next(seq)})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})

    return fake


async def test_vision_loop_types_then_finishes(monkeypatch):
    log: list[str] = []

    async def fake_get_page():
        return _Page(log)

    monkeypatch.setattr(va, "get_page", fake_get_page)
    monkeypatch.setattr(va.litellm, "acompletion", _llm(
        '{"act":"type","n":0,"text":"Linux kernel","enter":true}',
        '{"act":"done","answer":"1991"}',
    ))
    out = await web_task_vision("when was linux released", "https://x.test", max_steps=5)
    assert out["answer"] == "1991"
    assert out["steps"] == 2
    assert "fill:Linux kernel" in log and "press:Enter" in log  # it really acted


async def test_vision_hard_stops_on_loop(monkeypatch):
    async def fake_get_page():
        return _Page([])

    monkeypatch.setattr(va, "get_page", fake_get_page)
    monkeypatch.setattr(va.litellm, "acompletion", _llm(*['{"act":"scroll","dy":600}'] * 8))
    out = await web_task_vision("find it", "https://x.test", max_steps=8)
    assert out.get("stuck") is True and out["steps"] == 3
