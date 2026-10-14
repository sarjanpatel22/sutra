"""Tests for the email-spoofing skill (friday/skills/email_security.py) — dig (_txt) mocked."""

from __future__ import annotations

from friday.skills import email_security


def _patch(monkeypatch, records: dict):
    async def fake_txt(name):
        return records.get(name, [])

    monkeypatch.setattr(email_security, "_txt", fake_txt)


def _ids(out):
    return [f["id"] for f in out["findings"]]


async def test_missing_spf_and_dmarc(monkeypatch):
    _patch(monkeypatch, {})
    out = await email_security.check_email_spoofing("target.com")
    assert "spf-missing" in _ids(out) and "dmarc-missing" in _ids(out)


async def test_weak_spf_flagged(monkeypatch):
    _patch(monkeypatch, {"target.com": ["v=spf1 include:x +all"],
                         "_dmarc.target.com": ["v=DMARC1; p=reject"]})
    out = await email_security.check_email_spoofing("target.com")
    assert "spf-weak" in _ids(out) and out["vulnerable"] is True


async def test_dmarc_none_flagged(monkeypatch):
    _patch(monkeypatch, {"target.com": ["v=spf1 -all"],
                         "_dmarc.target.com": ["v=DMARC1; p=none; rua=mailto:x"]})
    out = await email_security.check_email_spoofing("target.com")
    assert "dmarc-none" in _ids(out) and "spf-weak" not in _ids(out)


async def test_strong_config_clean(monkeypatch):
    _patch(monkeypatch, {"target.com": ["v=spf1 include:x -all"],
                         "_dmarc.target.com": ["v=DMARC1; p=reject"]})
    out = await email_security.check_email_spoofing("target.com")
    assert out["findings"] == [] and out["vulnerable"] is False


async def test_dmarc_sp_none_not_mislabelled(monkeypatch):
    # p=reject with a subdomain policy sp=none is STRONG — the substring 'p=none' inside 'sp=none'
    # must not trip the monitor-only finding (the false-positive this guards against).
    _patch(monkeypatch, {"target.com": ["v=spf1 -all"],
                         "_dmarc.target.com": ["v=DMARC1; p=reject; sp=none; pct=100"]})
    out = await email_security.check_email_spoofing("target.com")
    assert "dmarc-none" not in _ids(out)
    assert out["findings"] == [] and out["vulnerable"] is False


async def test_spf_bare_all_flagged(monkeypatch):
    # a bare 'all' mechanism defaults to '+all' (pass everyone) — must be flagged permissive.
    _patch(monkeypatch, {"target.com": ["v=spf1 include:x all"],
                         "_dmarc.target.com": ["v=DMARC1; p=reject"]})
    out = await email_security.check_email_spoofing("target.com")
    assert "spf-weak" in _ids(out)


async def test_spf_softfail_not_flagged(monkeypatch):
    # ~all (soft fail) and -all (hard fail) are acceptable — not permissive.
    _patch(monkeypatch, {"target.com": ["v=spf1 include:x ~all"],
                         "_dmarc.target.com": ["v=DMARC1; p=reject"]})
    out = await email_security.check_email_spoofing("target.com")
    assert "spf-weak" not in _ids(out)


async def test_url_input_normalized(monkeypatch):
    _patch(monkeypatch, {})
    out = await email_security.check_email_spoofing("https://target.com/path")
    assert out["domain"] == "target.com"


async def test_no_domain():
    assert "error" in await email_security.check_email_spoofing("")


def test_check_email_spoofing_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("check_email_spoofing").auto_confirm is True  # passive DNS → auto
