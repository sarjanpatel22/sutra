"""Tests for the GitHub secret-recon skill (friday/skills/github_recon.py).

The gh code-search call (_gh_search) is mocked so confirmed-secret detection, noise filtering, and
dedupe are exercised deterministically — no gh CLI, no network.
"""

from __future__ import annotations

from friday.skills import github_recon


def _items(*fragments):
    return [
        {"repository": {"full_name": "acme/app"}, "path": "config.py",
         "html_url": "https://github.com/acme/app/blob/main/config.py",
         "text_matches": [{"fragment": f} for f in fragments]}
    ]


async def test_confirmed_secret_reported(monkeypatch):
    async def fake(query, per_page=20):
        return _items('AWS_KEY = "AKIAIOSFODNN7EXAMPLE"'), None

    monkeypatch.setattr(github_recon, "_gh_search", fake)
    out = await github_recon.scan_github("acme")
    assert out["count"] >= 1
    f = out["findings"][0]
    assert f["id"] == "github-leaked-secret" and "AWS access key" in f["name"]


async def test_keyword_mention_without_secret_ignored(monkeypatch):
    async def fake(query, per_page=20):
        # mentions 'password' but no actual secret value → must NOT be flagged
        return _items("// remember to set your password in the env file"), None

    monkeypatch.setattr(github_recon, "_gh_search", fake)
    out = await github_recon.scan_github("acme")
    assert out["count"] == 0 and out["findings"] == []


async def test_dedupes_same_repo_path_label(monkeypatch):
    async def fake(query, per_page=20):
        return _items('key="AKIAIOSFODNN7EXAMPLE"', 'key2="AKIAIOSFODNN7EXAMPLE"'), None

    monkeypatch.setattr(github_recon, "_gh_search", fake)
    out = await github_recon.scan_github("acme")
    assert out["count"] == 1  # same (repo, path, AWS access key) counted once


async def test_gh_absent_is_graceful(monkeypatch):
    async def fake(query, per_page=20):
        return [], "gh CLI not installed — install + authenticate it to search GitHub"

    monkeypatch.setattr(github_recon, "_gh_search", fake)
    out = await github_recon.scan_github("acme")
    assert "error" in out and "gh" in out["error"]


async def test_no_target():
    assert "error" in await github_recon.scan_github("")


def test_detect_recognizes_formats():
    assert github_recon._detect('x = "AKIAIOSFODNN7EXAMPLE"')[0] == "AWS access key"
    assert github_recon._detect("AIza" + "A" * 35)[0] == "Google API key"  # 39-char format
    assert github_recon._detect("-----BEGIN RSA PRIVATE KEY-----")[0] == "Private key"
    assert github_recon._detect("just some normal code here") is None


def test_scan_github_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("scan_github").auto_confirm is True  # queries GitHub, not the target
