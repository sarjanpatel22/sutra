"""Tests for `friday.skills.csp_analyze` — network mocked, the parser is pure."""

from __future__ import annotations

import pytest

from friday.skills import csp_analyze as ca
from friday.skills.csp_analyze import analyze, analyze_csp, parse_csp

# --- pure parser/analyser ----------------------------------------------------


def test_parse_csp_splits_directives() -> None:
    d = parse_csp("default-src 'self'; script-src 'self' 'unsafe-inline'; object-src 'none'")
    assert d["default-src"] == ["'self'"]
    assert d["script-src"] == ["'self'", "'unsafe-inline'"]
    assert d["object-src"] == ["'none'"]


def test_unsafe_inline_flagged_high() -> None:
    issues = analyze("script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'")
    assert any(i["issue"].startswith("script-src allows 'unsafe-inline'")
               and i["severity"] == "high" for i in issues)


def test_nonce_suppresses_unsafe_inline_finding() -> None:
    # With a nonce present, 'unsafe-inline' is ignored by browsers → don't flag it high.
    pol = "script-src 'unsafe-inline' 'nonce-abc123'; object-src 'none'; base-uri 'self'"
    issues = analyze(pol)
    assert not any("unsafe-inline" in i["issue"] for i in issues)


def test_wildcard_script_source_flagged() -> None:
    issues = analyze("script-src 'self' https:; object-src 'none'; base-uri 'none'")
    assert any("wildcard source 'https:'" in i["issue"] for i in issues)


def test_missing_base_uri_flagged() -> None:
    issues = analyze("script-src 'self'; object-src 'none'")
    assert any(i["issue"] == "no base-uri directive" for i in issues)


def test_strict_policy_minimal_issues() -> None:
    issues = analyze("default-src 'none'; script-src 'self'; object-src 'none'; base-uri 'none'")
    assert issues == []


# --- analyze_csp -------------------------------------------------------------


async def test_no_csp_header_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ca, "_fetch_csp", lambda url: (None, 200))
    out = await analyze_csp("http://x.com")
    assert out["has_csp"] is False
    assert "no CSP defence" in out["note"]


async def test_weak_policy_returns_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ca, "_fetch_csp",
                        lambda url: ("script-src 'self' 'unsafe-inline'", 200))
    out = await analyze_csp("x.com")  # also exercises scheme-prepend
    assert out["has_csp"] is True
    assert out["issue_count"] >= 1
    assert any(i["severity"] == "high" for i in out["issues"])


async def test_unreachable_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ca, "_fetch_csp", lambda url: "could not reach http://x.com: refused")
    out = await analyze_csp("http://x.com")
    assert "error" in out


async def test_empty_url_errors() -> None:
    assert "error" in await analyze_csp("")


def test_skill_is_auto_confirmed() -> None:
    from friday.skills import registry
    registry.autodiscover()
    assert registry.get("analyze_csp").auto_confirm is True
