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


def test_minified_far_apart_source_sink_not_high() -> None:
    # The whole minified file is ONE line: a source and a sink separated by kilobytes must NOT be
    # a high flow (the bug that flagged 19 on an Angular bundle). May be a single LOW, never high.
    line = "var a=location.hash;" + ("x;" * 2000) + "el.innerHTML=untrusted;"
    out = _scan_js("main.min.js", line)
    assert all(f["confidence"] != "high" for f in out)


def test_minified_close_source_sink_still_high() -> None:
    # A real flow on a minified line (source within proximity of the sink) IS still caught.
    line = ("noise;" * 500) + "el.innerHTML=location.hash;" + ("noise;" * 500)
    out = _scan_js("main.min.js", line)
    assert any(f["confidence"] == "high" for f in out)


def test_single_line_cannot_flood_with_highs() -> None:
    # Many real-looking flows on one minified line are capped so one line can't dominate.
    line = "el.innerHTML=location.hash;" * 20
    out = _scan_js("main.min.js", line)
    highs = [f for f in out if f["confidence"] == "high"]
    assert len(highs) <= dx._MAX_HIGH_PER_LINE


def test_bare_hash_or_search_method_not_a_source() -> None:
    # The tightened source regex must NOT treat String.search / Array.hash-like as a DOM source.
    assert _scan_js("app.js", "el.innerHTML = items.search(term);") == []
    assert _scan_js("app.js", "node.innerHTML = obj.hash;") == []


def test_lowercase_function_expression_is_not_a_sink() -> None:
    # `function(` expressions are everywhere; only the capital-F `Function()` constructor is a sink.
    assert _scan_js("app.js", "arr.map(function(x){ return location.hash; });") == []
    out = _scan_js("app.js", "var f = new Function(location.hash);")  # the constructor IS a sink
    assert any(c["confidence"] == "high" for c in out)


def test_bare_location_does_not_self_match_navigation_sink() -> None:
    # `location.replace("/home")` must not be read as source `location` → sink `location.replace`.
    assert all(c["confidence"] != "high"
               for c in _scan_js("app.js", 'window.location.replace("/home");'))


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


async def test_scan_skips_framework_bundles(monkeypatch: pytest.MonkeyPatch) -> None:
    # A vendor/framework bundle is skipped (its sinks are library internals); the app bundle isn't.
    page = ('<html><script src="/static/vendor.1a2b.js"></script>'
            '<script src="/static/polyfills.js"></script>'
            '<script src="/static/main.js"></script></html>')
    bodies = {
        "vendor.1a2b.js": "el.innerHTML=location.hash;",     # would flag, but must be skipped
        "polyfills.js": "document.write(location.search);",  # skipped too
        "main.js": "node.innerHTML=location.hash;",          # the app bundle — scanned
    }

    def fake(url: str) -> tuple[str, str | None]:
        if url.rstrip("/").endswith("example.com"):
            return page, None
        return bodies.get(url.split("/")[-1], ""), None

    monkeypatch.setattr(dx, "_fetch", fake)
    out = await dom_xss_scan("http://example.com/")
    assert out["skipped_bundles"] == 2
    assert out["scripts_scanned"] == 1
    files = {c["file"] for c in out["candidates"]}
    assert "main.js" in files
    assert not any("vendor" in f or "polyfill" in f for f in files)


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
