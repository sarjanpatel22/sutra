"""Tests for `friday.skills.shodan_recon` — keyring/network fully mocked, no real Shodan calls."""

from __future__ import annotations

import pytest

from friday.skills import shodan_recon as sr
from friday.skills.shodan_recon import shodan_host

_SAMPLE = {
    "ip_str": "1.2.3.4",
    "hostnames": ["host.example.com"],
    "org": "Example Org",
    "isp": "Example ISP",
    "os": "Linux",
    "ports": [80, 443, 22],
    "vulns": ["CVE-2021-44228", "CVE-2014-0160"],
    "data": [
        {"port": 22, "transport": "tcp", "product": "OpenSSH", "version": "7.4",
         "data": "SSH-2.0-OpenSSH_7.4"},
        {"port": 443, "transport": "tcp", "product": "nginx", "version": "1.18.0", "data": ""},
    ],
}


def _key(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    monkeypatch.setattr(sr, "get_secret", lambda name, default=None: value)


def _resolve_ok(monkeypatch: pytest.MonkeyPatch, ip: str = "1.2.3.4") -> None:
    monkeypatch.setattr(sr, "_resolve", lambda t: (ip, None))


# --- gating ------------------------------------------------------------------


async def test_no_key_degrades_with_instruction(monkeypatch: pytest.MonkeyPatch) -> None:
    _key(monkeypatch, None)
    out = await shodan_host("example.com")
    assert "error" in out and "SHODAN_API_KEY" in out["error"]


async def test_empty_target_errors() -> None:
    assert "error" in await shodan_host("")


# --- happy path --------------------------------------------------------------


async def test_returns_ports_services_vulns(monkeypatch: pytest.MonkeyPatch) -> None:
    _key(monkeypatch, "k")
    _resolve_ok(monkeypatch)
    monkeypatch.setattr(sr, "_lookup", lambda ip, key: dict(_SAMPLE))
    out = await shodan_host("example.com")
    assert out["ip"] == "1.2.3.4"
    assert out["ports"] == [22, 80, 443]  # sorted
    assert out["vulns"] == ["CVE-2014-0160", "CVE-2021-44228"]  # sorted
    assert out["count"] == 2
    assert out["services"][0]["product"] == "OpenSSH"
    assert out["services"][1]["banner"] is None  # empty banner normalised to None


async def test_404_means_no_record(monkeypatch: pytest.MonkeyPatch) -> None:
    _key(monkeypatch, "k")
    _resolve_ok(monkeypatch)
    monkeypatch.setattr(sr, "_lookup", lambda ip, key: {"_none": True})
    out = await shodan_host("1.2.3.4")
    assert out["count"] == 0
    assert "no Shodan record" in out["note"]


async def test_bad_key_surfaces_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _key(monkeypatch, "bad")
    _resolve_ok(monkeypatch)
    monkeypatch.setattr(
        sr, "_lookup",
        lambda ip, key: {"_error": "Shodan rejected the API key (401) — check SHODAN_API_KEY"})
    out = await shodan_host("1.2.3.4")
    assert "401" in out["error"]


async def test_unresolvable_target_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _key(monkeypatch, "k")
    monkeypatch.setattr(sr, "_resolve", lambda t: (None, "could not resolve nope.invalid"))
    out = await shodan_host("nope.invalid")
    assert "error" in out and "resolve" in out["error"]


def test_skill_is_auto_confirmed() -> None:
    from friday.skills import registry
    registry.autodiscover()
    assert registry.get("shodan_host").auto_confirm is True
