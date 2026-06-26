"""Tests for the subdomain-takeover skill (friday/skills/takeover.py).

CNAME + HTTP fetch are mocked (host normalization strips the port, so a real local server won't be
reached anyway) — this exercises the fingerprint-matching logic deterministically, no network/dig.
"""

from __future__ import annotations

from friday.skills import takeover


def _patch(monkeypatch, cname: str, body: str):
    async def fake_cname(host):
        return cname

    monkeypatch.setattr(takeover, "_cname", fake_cname)
    monkeypatch.setattr(takeover, "_fetch_body", lambda host: body)


def _ids(out):
    return [f["id"] for f in out["findings"]]


async def test_takeover_detected_by_body_and_cname(monkeypatch):
    _patch(monkeypatch, "fancyuser.github.io", "Oops! There isn't a GitHub Pages site here.")
    out = await takeover.check_takeover("blog.target.com")
    assert out["vulnerable"] is True
    f = next(f for f in out["findings"] if f["id"] == "subdomain-takeover")
    assert f["severity"] == "high" and "GitHub Pages" in f["name"]
    assert "github.io" in f["description"]  # CNAME corroboration included


async def test_body_fingerprint_without_cname(monkeypatch):
    _patch(monkeypatch, "", "<Error><Code>NoSuchBucket</Code></Error>")
    out = await takeover.check_takeover("assets.target.com")
    assert out["vulnerable"] is True
    f = next(f for f in out["findings"] if f["id"] == "subdomain-takeover")
    assert "AWS S3" in f["name"] and "CNAME not resolved" in f["description"]


async def test_cname_candidate_when_body_not_unclaimed(monkeypatch):
    _patch(monkeypatch, "someproject.herokuapp.com", "<html>a perfectly normal live app</html>")
    out = await takeover.check_takeover("app.target.com")
    assert out["vulnerable"] is False
    assert "subdomain-takeover-candidate" in _ids(out)  # points to Heroku, verify it's claimed


async def test_clean_host_no_findings(monkeypatch):
    _patch(monkeypatch, "", "<html>normal first-party homepage</html>")
    out = await takeover.check_takeover("www.target.com")
    assert out["findings"] == [] and out["vulnerable"] is False


async def test_inconclusive_when_no_cname_and_no_body(monkeypatch):
    _patch(monkeypatch, "", "")
    out = await takeover.check_takeover("dead.target.com")
    assert out["findings"] == [] and "Inconclusive" in out["note"]


async def test_url_input_normalized_to_hostname(monkeypatch):
    _patch(monkeypatch, "", "")
    out = await takeover.check_takeover("https://sub.target.com:8443/some/path")
    assert out["host"] == "sub.target.com"  # scheme/port/path stripped


async def test_no_host():
    assert "error" in await takeover.check_takeover("")


def test_check_takeover_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("check_takeover").auto_confirm is True
