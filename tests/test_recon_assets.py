"""Tests for the recon_assets meta-skill (friday/skills/recon_assets.py).

The two underlying sources are mocked so the merge/dedupe/overlap logic and graceful degradation
when a source fails are exercised deterministically — no network, no subfinder binary.
"""

from __future__ import annotations

from friday.skills import recon_assets


def _patch(monkeypatch, sub, crt):
    from friday.skills import crtsh, recon

    async def fake_sub(domain):
        return sub

    async def fake_crt(domain):
        return crt

    monkeypatch.setattr(recon, "find_subdomains", fake_sub)
    monkeypatch.setattr(crtsh, "crtsh_subdomains", fake_crt)


async def test_merges_and_dedupes(monkeypatch):
    _patch(monkeypatch,
           {"subdomains": ["www.x.com", "api.x.com"]},
           {"subdomains": ["api.x.com", "vpn.x.com"]})  # api.x.com overlaps
    out = await recon_assets.recon_assets("x.com")
    assert out["total"] == 3                                   # union, deduped
    assert set(out["subdomains"]) == {"www.x.com", "api.x.com", "vpn.x.com"}
    assert out["by_source"] == {"subfinder": 2, "crtsh": 2}


async def test_crt_only_highlighted(monkeypatch):
    _patch(monkeypatch,
           {"subdomains": ["www.x.com"]},
           {"subdomains": ["www.x.com", "secret.x.com"]})
    out = await recon_assets.recon_assets("x.com")
    assert out["crt_only"] == ["secret.x.com"]                 # CT caught what subfinder missed


async def test_one_source_failing_is_graceful(monkeypatch):
    _patch(monkeypatch,
           {"error": "subfinder not installed"},
           {"subdomains": ["a.x.com", "b.x.com"]})
    out = await recon_assets.recon_assets("x.com")
    assert out["total"] == 2                                   # used the working source
    assert "subfinder" in out["errors"]                       # failure surfaced, not silent


async def test_no_domain():
    assert "error" in await recon_assets.recon_assets("")


def test_recon_assets_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("recon_assets").auto_confirm is True
