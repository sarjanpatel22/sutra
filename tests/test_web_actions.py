"""Tests for gated web-action skills (fill_form).

Live form submission is exercised by a manual smoke; here we lock the guardrails that
matter: fill_form is irreversible so it MUST be gated, and a bad URL or bad `fields`
type is refused before any browser launches.
"""

from friday.skills import registry
from friday.skills.web_actions import fill_form


def test_fill_form_is_gated():
    # Irreversible (a submit may POST data) → must require confirmation.
    registry.autodiscover()
    assert registry.get("fill_form").auto_confirm is False


async def test_fill_form_rejects_non_http():
    out = await fill_form("file:///etc/passwd", {"#q": "x"})
    assert "http(s)" in out["error"]


async def test_fill_form_rejects_bad_fields_type():
    out = await fill_form("https://example.com", fields="not-a-dict")  # type: ignore[arg-type]
    assert "map of CSS selector" in out["error"]
