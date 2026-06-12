"""Tests for the autonomous web agent (web_task).

The live loop (LLM + real browser) is exercised by a manual smoke; here we lock the
offline-checkable parts: the action JSON parser, the input guards, and that an
autonomous browse-and-act run is gated.
"""

from friday.skills import registry
from friday.skills.browser_agent import _parse_action, web_task


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
