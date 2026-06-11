"""Tests for the browse_url skill — URL safety + readable-text shaping.

Live browsing (Playwright + chromium) is exercised by a manual smoke, not the suite,
to keep tests fast and offline. Here we lock the read-only guardrails: only http(s)
URLs reach the browser, and the text normalizer strips blank-line noise.
"""

from friday.skills.browse import _http_url, _readable, browse_url


def test_http_url_accepts_http_and_https():
    assert _http_url("http://example.com")
    assert _http_url("https://example.com/path?q=1")


def test_http_url_rejects_dangerous_schemes():
    for bad in (
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,x",
        "ftp://host/x",
        "notaurl",
        "",
    ):
        assert not _http_url(bad), bad


def test_readable_strips_blank_lines_and_whitespace():
    raw = "  Title \n\n\n  body line  \n   \nlast\n"
    assert _readable(raw) == "Title\nbody line\nlast"


async def test_browse_url_rejects_non_http_without_launching():
    # A bad scheme returns an error before Playwright is ever imported/launched.
    out = await browse_url("file:///etc/passwd")
    assert "http(s)" in out["error"]
