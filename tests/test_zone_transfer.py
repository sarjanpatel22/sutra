"""Tests for the DNS zone-transfer skill (friday/skills/zone_transfer.py) - dig (_run) mocked."""

from __future__ import annotations

from friday.skills import zone_transfer

_ZONE = ("target.com.\t300\tIN\tSOA\tns1.target.com. root. 1 2 3 4 5\n"
         "target.com.\t300\tIN\tA\t1.2.3.4\n"
         "www.target.com.\t300\tIN\tA\t1.2.3.5\n"
         "secret.target.com.\t300\tIN\tA\t10.0.0.9\n")


def _patch(monkeypatch, ns_out: str, axfr):
    monkeypatch.setattr(zone_transfer.shutil, "which", lambda b: "/usr/bin/dig")

    async def fake_run(cmd, timeout=20):
        if "AXFR" in cmd:
            host = next((a[1:] for a in cmd if a.startswith("@")), "")
            return (0, axfr.get(host, "; Transfer failed."), "")
        if "NS" in cmd:
            return (0, ns_out, "")
        return (0, "", "")

    monkeypatch.setattr(zone_transfer, "_run", fake_run)


def _ids(out):
    return [f["id"] for f in out["findings"]]


async def test_zone_transfer_allowed_flagged(monkeypatch):
    _patch(monkeypatch, "ns1.target.com.\nns2.target.com.\n",
           {"ns1.target.com": _ZONE, "ns2.target.com": "; Transfer failed."})
    out = await zone_transfer.dns_zone_transfer("target.com")
    assert out["vulnerable"] is True and "dns-zone-transfer" in _ids(out)
    r1 = next(r for r in out["results"] if r["nameserver"] == "ns1.target.com")
    assert r1["transferred"] is True and r1["records"] >= 2


async def test_no_transfer_when_all_denied(monkeypatch):
    _patch(monkeypatch, "ns1.target.com.\nns2.target.com.\n",
           {"ns1.target.com": "; Transfer failed.", "ns2.target.com": "; Transfer failed."})
    out = await zone_transfer.dns_zone_transfer("target.com")
    assert out["vulnerable"] is False and out["findings"] == []


async def test_no_nameservers(monkeypatch):
    _patch(monkeypatch, "", {})
    out = await zone_transfer.dns_zone_transfer("target.com")
    assert out["findings"] == [] and "No NS records" in out["note"]


async def test_dig_missing(monkeypatch):
    monkeypatch.setattr(zone_transfer.shutil, "which", lambda b: None)
    out = await zone_transfer.dns_zone_transfer("target.com")
    assert "error" in out and "dig" in out["error"]


async def test_url_input_normalized(monkeypatch):
    _patch(monkeypatch, "", {})
    out = await zone_transfer.dns_zone_transfer("https://target.com/path")
    assert out["domain"] == "target.com"


async def test_no_domain():
    assert "error" in await zone_transfer.dns_zone_transfer("")


def test_dns_zone_transfer_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("dns_zone_transfer").auto_confirm is True  # read-only DNS -> auto
