"""Tests for `friday.skills.dom_xss` — fetch fully mocked, no real network."""

from __future__ import annotations

import pytest

from friday.skills import dom_xss as dx
from friday.skills.dom_xss import _scan_js, dom_xss_scan

_VULN_JS = """
function render() {
  var q = location.hash.substring(1);
  document.getElementById('out').innerHTML = q;   // source+sink same line? no
  el.innerHTML = location.hash;                    // high: source+sink one line
}
eval(window.name);                                 // high: source+sink one line
"""

_SINK_ONLY_JS = "el.innerHTML = templateString;"  # sink, no source anywhere → not flagged

_LOW_JS = """
var data = location.search;
// ... lots of code ...
node.innerHTML = sanitize(data);
"""


# --- _scan_js ----------------------------------------------------------------


def test_scan_flags_source_and_sink_same_line_as_high() -> None:
    out = _scan_js("app.js", "el.innerHTML = location.hash;")
    assert len(out) == 1
    assert out[0]["confidence"] == "high"
    assert out[0]["sink"].lower().startswith("innerhtml")
    assert "location.hash" in out[0]["source"].lower()


def test_scan_eval_window_name_high() -> None:
    out = _scan_js("app.js", "eval(window.name);")
    assert out and out[0]["confidence"] == "high"


def test_scan_sink_without_any_source_is_not_flagged() -> None:
    assert _scan_js("app.js", _SINK_ONLY_JS) == []


def test_scan_sink_with_source_elsewhere_is_low() -> None:
    out = _scan_js("app.js", _LOW_JS)
    assert any(f["confidence"] == "low" and "elsewhere" in f["source"] for f in out)


def test_scan_reports_file_line_snippet() -> None:
    out = _scan_js("bundle.js", "x.innerHTML = document.URL;")
    assert out[0]["file"] == "bundle.js"
    assert out[0]["line"] == 1
    assert "innerHTML" in out[0]["snippet"]


# --- dom_xss_scan ------------------------------------------------------------


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, pages: dict[str, str]) -> None:
    def fake(url: str) -> tuple[str, str | None]:
        for key, body in pages.items():
            if key in url:
                return body, None
        return "", "unreachable"
    monkeypatch.setattr(dx, "_fetch", fake)


async def test_scan_finds_flows_in_inline_and_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    page = '<html><script src="/static/app.js"></script>' + \
           "<script>el.innerHTML = location.hash;</script></html>"
    _patch_fetch(monkeypatch, {
        "example.com/": page,
        "app.js": "document.write(location.search);",
    })
    out = await dom_xss_scan("http://example.com/")
    assert out["scripts_scanned"] == 1
    assert out["high_confidence"] >= 2  # inline innerHTML + bundle document.write
    assert any(c["file"] == "app.js" for c in out["candidates"])


async def test_scan_unreachable_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dx, "_fetch", lambda url: ("", "boom"))
    out = await dom_xss_scan("http://nope.invalid/")
    assert "error" in out


async def test_scan_clean_page_no_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetch(monkeypatch, {"example.com": "<html><body>hello</body></html>"})
    out = await dom_xss_scan("example.com")  # also exercises scheme-prepend
    assert out["count"] == 0
    assert out["candidates"] == []


async def test_scan_empty_url_errors() -> None:
    assert "error" in await dom_xss_scan("")


def test_skill_is_auto_confirmed() -> None:
    from friday.skills import registry
    registry.autodiscover()
    assert registry.get("dom_xss_scan").auto_confirm is True
