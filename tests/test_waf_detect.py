"""Tests for `friday.skills.waf_detect` — network fully mocked, no real requests."""

from __future__ import annotations

import pytest

from friday.skills import waf_detect as wd
from friday.skills.waf_detect import _match, detect_waf


def _meta(monkeypatch, headers, cookie_raw="", status=200):
    low = {k.lower(): v for k, v in headers.items()}
    monkeypatch.setattr(wd, "_fetch_meta", lambda url: (low, cookie_raw, status))


# --- _match ------------------------------------------------------------------


def test_match_cloudflare_by_header() -> None:
    found = _match({"server": "cloudflare", "cf-ray": "abc123"}, set())
    assert any(d["waf"] == "Cloudflare" for d in found)


def test_match_imperva_by_cookie() -> None:
    found = _match({}, {"incap_ses_1234_5678", "visid_incap_5678"})
    assert any(d["waf"] == "Imperva Incapsula" for d in found)


def test_match_akamai_by_cookie_prefix() -> None:
    found = _match({}, {"ak_bmsc"})
    assert any(d["waf"] == "Akamai" for d in found)


def test_match_nothing_when_clean() -> None:
    assert _match({"server": "nginx", "content-type": "text/html"}, {"sessionid"}) == []


def test_match_header_presence_only() -> None:
    # x-iinfo present with ANY value → Imperva (needle == "")
    found = _match({"x-iinfo": "9-12345-0 NNNN"}, set())
    assert any(d["waf"] == "Imperva Incapsula" for d in found)


# --- detect_waf --------------------------------------------------------------


async def test_detect_reports_waf_with_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    _meta(monkeypatch, {"Server": "cloudflare", "CF-RAY": "7a"}, status=200)
    out = await detect_waf("https://x.com")
    assert out["waf_detected"] is True
    assert out["wafs"][0]["waf"] == "Cloudflare"
    assert "evidence" in out["wafs"][0]


async def test_detect_dedupes_multiple_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two Cloudflare signals (server + cf-ray) must collapse to ONE entry.
    _meta(monkeypatch, {"server": "cloudflare", "cf-ray": "1", "cf-cache-status": "HIT"})
    out = await detect_waf("x.com")  # also exercises scheme-prepend
    assert [d["waf"] for d in out["wafs"]] == ["Cloudflare"]


async def test_detect_clean_target_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    _meta(monkeypatch, {"server": "nginx"})
    out = await detect_waf("https://x.com")
    assert out["waf_detected"] is False
    assert "absence isn't proof" in out["note"]


async def test_detect_unreachable_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wd, "_fetch_meta", lambda url: "could not reach https://x.com: refused")
    out = await detect_waf("https://x.com")
    assert "error" in out


async def test_detect_empty_url_errors() -> None:
    assert "error" in await detect_waf("")


def test_skill_is_auto_confirmed() -> None:
    from friday.skills import registry
    registry.autodiscover()
    assert registry.get("detect_waf").auto_confirm is True
