"""Tests for `sutra hunt` — the one-command program hunt (enumerate → probe → scan → report)."""

from friday.core import hunt as H


async def test_is_live_web_detects_open_port(monkeypatch):
    class _W:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def only_443(host, port):
        if port == 443:
            return object(), _W()
        raise OSError("refused")

    monkeypatch.setattr(H.asyncio, "open_connection", only_443)
    assert await H._is_live_web("x.com") == "https://x.com"

    async def dead(host, port):
        raise OSError("refused")

    monkeypatch.setattr(H.asyncio, "open_connection", dead)
    assert await H._is_live_web("dead.com") is None


async def test_hunt_single_target_scans_one_no_enum(monkeypatch):
    from friday.skills import web_recon

    async def fake_nuclei(target, severity="", tags=""):
        return {"findings": [{"id": "x", "name": "X", "severity": "medium", "matched_at": target}]}

    monkeypatch.setattr(web_recon, "nuclei_scan", fake_nuclei)
    res = await H.hunt("http://localhost:3000")          # a URL → single target, auto no-enum
    assert res["hosts"] == ["http://localhost:3000"]
    assert len(res["findings"]) == 1
    assert res["findings"][0]["host"] == "http://localhost:3000"   # finding tagged with its host
    assert "Bug-bounty report" in res["report"]


async def test_hunt_bare_domain_enumerates_and_chains(monkeypatch):
    from friday.skills import recon, web_recon

    async def fake_find(domain):
        assert domain == "example.com"
        return {"subdomains": ["a.example.com", "b.example.com"]}

    async def fake_live(hosts, max_hosts):
        return ["https://a.example.com"]                 # only one of them answers

    async def fake_nuclei(target, severity="", tags=""):
        return {"findings": [{"id": "y", "name": "Y", "severity": "high", "matched_at": target}]}

    monkeypatch.setattr(recon, "find_subdomains", fake_find)
    monkeypatch.setattr(H, "_live_web_hosts", fake_live)
    monkeypatch.setattr(web_recon, "nuclei_scan", fake_nuclei)

    res = await H.hunt("example.com")                    # bare domain → auto-enumerate
    assert res["hosts"] == ["https://a.example.com"]     # only the live one is scanned
    assert len(res["findings"]) == 1 and res["findings"][0]["host"] == "https://a.example.com"


async def test_hunt_empty_target():
    assert "error" in await H.hunt("")
