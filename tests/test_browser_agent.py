"""Tests for the autonomous web agent (web_task).

The live loop (LLM + real browser) is exercised by a manual smoke; here we lock the
offline-checkable parts: the action JSON parser, the input guards, and that an
autonomous browse-and-act run is gated.
"""

import friday.skills.browser_agent as ba
from friday.skills import registry
from friday.skills.browser_agent import _history_block, _parse_action, _signature, web_task


def test_history_block_empty_and_populated():
    assert "nothing yet" in _history_block([])
    block = _history_block([
        {"action": {"action": "type", "index": 2, "text": "linux"}},
        {"action": {"action": "click", "index": 5}, "error": "element gone"},
    ])
    assert "type" in block and "linux" in block and "ERROR: element gone" in block


def test_signature_distinguishes_actions():
    a = _signature({"action": "scroll", "amount": 700})
    assert a == _signature({"action": "scroll", "amount": 700})  # stable
    assert a != _signature({"action": "scroll", "amount": -700})  # target-sensitive
    assert a != _signature({"action": "click", "index": 1})


def test_parse_action_plain_json():
    assert _parse_action('{"action":"done","answer":"hi"}') == {"action": "done", "answer": "hi"}


def test_parse_action_embedded_in_prose():
    out = _parse_action('Sure, I will do: {"action":"click","index":3} now.')
    assert out == {"action": "click", "index": 3}


def test_parse_action_rejects_non_action_and_garbage():
    assert _parse_action("no json here at all") is None
    assert _parse_action('{"foo": 1}') is None  # valid JSON but no "action" key
    assert _parse_action("{not valid json}") is None


def test_web_task_is_gated():
    registry.autodiscover()
    assert registry.get("web_task").auto_confirm is False


async def test_web_task_requires_goal():
    assert "goal" in (await web_task("", "https://example.com"))["error"]


async def test_web_task_requires_http_start_url():
    out = await web_task("do something", "file:///etc/passwd")
    assert "http" in out["error"].lower()


class _FakeHandle:
    def __init__(self, clicks):
        self._clicks = clicks

    async def is_visible(self):
        return True

    async def evaluate(self, _js):
        return "BUTTON"

    async def inner_text(self):
        return "Start"

    async def get_attribute(self, _a):
        return None

    async def click(self, **k):
        self._clicks.append(True)

    async def fill(self, _text):
        return None

    async def press(self, _key):
        return None


class _FakePage:
    """Minimal Playwright Page stand-in: one clickable button, fixed text."""

    url = "https://site.test/"

    def __init__(self, clicks):
        self._clicks = clicks
        self.mouse = self
        self.keyboard = self
        self.wheels = 0

    async def goto(self, url, **k):
        type(self).url = url

    async def title(self):
        return "Test Site"

    async def query_selector_all(self, _sel):
        return [_FakeHandle(self._clicks)]

    async def inner_text(self, _sel):
        return "Welcome. Click start to begin."

    async def wait_for_load_state(self, *a, **k):
        return None

    async def press(self, _key):  # keyboard.press
        return None

    async def wheel(self, *a, **k):
        self.wheels += 1


def _llm_returning(*replies):
    seq = iter(replies)

    async def fake_complete(messages, **k):
        text = next(seq)
        msg = type("M", (), {"content": text})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})

    return fake_complete


async def test_web_task_clicks_then_finishes(monkeypatch):
    """A scripted run: the agent clicks element 0, then declares done — and it drives
    Sutra's real browser via get_page, not a throwaway headless one."""
    clicks: list[bool] = []

    async def fake_get_page():
        return _FakePage(clicks)

    monkeypatch.setattr(ba, "get_page", fake_get_page)
    monkeypatch.setattr(
        ba, "complete",
        _llm_returning('{"action":"click","index":0}', '{"action":"done","answer":"started"}'),
    )
    out = await web_task("start the first item", "https://site.test", max_steps=5)
    assert out["answer"] == "started"
    assert clicks == [True]  # it really actuated the page
    assert out["steps"] == 2


async def test_web_task_runs_to_max_steps_when_model_loops(monkeypatch):
    """If the model keeps emitting the same scroll, the run ends at max_steps instead
    of hanging — the loop guard nudges but never deadlocks."""
    page = _FakePage([])

    async def fake_get_page():
        return page

    monkeypatch.setattr(ba, "get_page", fake_get_page)
    monkeypatch.setattr(ba, "complete", _llm_returning(*['{"action":"scroll","amount":700}'] * 4))
    out = await web_task("find the thing", "https://site.test", max_steps=4)
    assert out["steps"] == 4 and page.wheels == 4  # acted each step, then stopped


async def test_web_task_reports_browser_launch_failure(monkeypatch):
    async def boom():
        raise RuntimeError("no display")

    monkeypatch.setattr(ba, "get_page", boom)
    out = await web_task("do it", "https://site.test")
    assert "could not start" in out["error"]
