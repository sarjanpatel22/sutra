"""Tests for `friday.skills.dns_brute` — resolver fully mocked, no real DNS."""

from __future__ import annotations

import pytest

from friday.skills import dns_brute as db
from friday.skills.dns_brute import _bare, _candidates, _label, dns_brute


def _fake_resolver(table: dict[str, list[str]]):
    """Build a fake _resolve that answers from `table` (host → ips), empty otherwise."""
    async def fake(host: str, sem: object) -> tuple[str, list[str]]:
        return host, list(table.get(host, []))
    return fake


# --- helpers -----------------------------------------------------------------


def test_bare_strips_scheme_path_port() -> None:
    assert _bare("https://API.Example.com:8443/path") == "api.example.com"
    assert _bare("example.com") == "example.com"


def test_label_extracts_leftmost_sub() -> None:
    assert _label("api.example.com", "example.com") == "api"
    assert _label("a.b.example.com", "example.com") == "a"
    assert _label("example.com", "example.com") == ""  # apex has no sub-label
    assert _label("api.other.com", "example.com") == ""  # different domain


def test_candidates_include_common_and_are_bounded() -> None:
    cands = _candidates("example.com", [], [], permute=False)
    assert "www.example.com" in cands
    assert "admin.example.com" in cands
    assert all(c.endswith(".example.com") for c in cands)
    assert len(cands) <= db._MAX_CANDIDATES


def test_candidates_permute_known_subs() -> None:
    cands = _candidates("example.com", [], ["api.example.com"], permute=True)
    assert "dev-api.example.com" in cands
    assert "api-bak.example.com" in cands
    assert "api2.example.com" in cands


def test_candidates_custom_words_merged() -> None:
    cands = _candidates("example.com", ["zzcustom"], [], permute=False)
    assert "zzcustom.example.com" in cands


# --- dns_brute ---------------------------------------------------------------


async def test_brute_returns_only_resolving_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {"www.example.com": ["93.184.216.34"], "admin.example.com": ["10.0.0.5"]}
    monkeypatch.setattr(db, "_resolve", _fake_resolver(table))
    out = await dns_brute("example.com", permute=False)
    hosts = {r["host"] for r in out["subdomains"]}
    assert hosts == {"www.example.com", "admin.example.com"}
    assert out["count"] == 2
    assert out["tried"] >= 2


async def test_brute_filters_wildcard_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every probe (incl. the random wildcard probes) returns the same catch-all IP, except a
    # real host that returns a distinct IP. Only the real host should survive.
    wildcard_ip = "203.0.113.99"
    real = {"api.example.com": ["198.51.100.7"]}

    async def fake(host: str, sem: object) -> tuple[str, list[str]]:
        if host in real:
            return host, real[host]
        if "sutra-wild-" in host:
            return host, [wildcard_ip]
        return host, [wildcard_ip]  # catch-all answers everything

    monkeypatch.setattr(db, "_resolve", fake)
    out = await dns_brute("example.com", permute=False)
    assert wildcard_ip in out["wildcard"]
    hosts = {r["host"] for r in out["subdomains"]}
    assert hosts == {"api.example.com"}  # wildcard noise filtered out


async def test_brute_rejects_bad_domain() -> None:
    assert "error" in await dns_brute("not-a-domain")
    assert "error" in await dns_brute("")


def test_skill_is_auto_confirmed() -> None:
    from friday.skills import registry
    registry.autodiscover()
    assert registry.get("dns_brute").auto_confirm is True
