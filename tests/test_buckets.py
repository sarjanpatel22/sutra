"""Tests for the open-bucket skill (friday/skills/buckets.py).

The provider probe (_probe) is mocked so candidate generation, public-vs-private classification, and
findings shaping are exercised deterministically — no requests to AWS/GCS.
"""

from __future__ import annotations

from friday.skills import buckets

_LIST = '<?xml version="1.0"?><ListBucketResult><Contents><Key>backup.sql</Key></Contents>' \
        "</ListBucketResult>"
_DENIED = "<Error><Code>AccessDenied</Code></Error>"


def _patch(monkeypatch, responder):
    monkeypatch.setattr(buckets, "_probe", responder)


async def test_public_bucket_is_a_finding(monkeypatch):
    def fake(url):
        # only example-backup on S3 is world-readable
        if "example-backup.s3.amazonaws.com" in url:
            return 200, _LIST, None
        return 404, "<Error><Code>NoSuchBucket</Code></Error>", None

    _patch(monkeypatch, fake)
    out = await buckets.check_buckets("example.com")
    assert out["vulnerable"] is True
    f = out["findings"][0]
    assert f["id"] == "open-bucket" and f["severity"] == "high"
    assert "example-backup" in f["name"] and "S3" in f["name"]


async def test_private_bucket_is_a_lead_not_finding(monkeypatch):
    def fake(url):
        if "example.s3.amazonaws.com" in url:
            return 403, _DENIED, None
        return 404, "NoSuchBucket", None

    _patch(monkeypatch, fake)
    out = await buckets.check_buckets("example.com")
    assert out["vulnerable"] is False                 # private = not a confirmed exposure
    assert any(b["bucket"] == "example" for b in out["exists_private"])  # but surfaced as a lead


async def test_no_buckets_found(monkeypatch):
    _patch(monkeypatch, lambda url: (404, "NoSuchBucket", None))
    out = await buckets.check_buckets("example.com")
    assert out["findings"] == [] and out["public"] == []


def test_candidate_generation_rules():
    names = buckets._candidates("example.com", extra="acme-corp")
    assert "example-backup" in names           # base + affix
    assert "backup-example" in names           # prefix + base
    assert "example-com" in names              # dotted form
    assert "acme-corp-assets" in names         # extra base used
    assert all("_" not in n and 3 <= len(n) <= 63 for n in names)  # S3-valid only


async def test_extra_only_no_domain(monkeypatch):
    _patch(monkeypatch, lambda url: (404, "", None))
    out = await buckets.check_buckets("", extra="acmecorp")
    assert "error" not in out and out["checked"] > 0


async def test_no_input():
    assert "error" in await buckets.check_buckets("")


def test_check_buckets_is_gated():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("check_buckets").auto_confirm is False  # active enumeration → gated
