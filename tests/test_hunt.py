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


async def _alive(host, timeout=2.0):
    return f"http://{host}"


async def _reachable(url, timeout=3.0):
    return True


async def test_hunt_single_target_scans_one_no_enum(monkeypatch):
    from friday.skills import web_recon

    async def fake_nuclei(target, severity="", tags=""):
        return {"findings": [{"id": "x", "name": "X", "severity": "medium", "matched_at": target}]}

    monkeypatch.setattr(H, "_url_reachable", _reachable)  # port-aware liveness gate
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


def test_scope_matching():
    assert H._matches("api.example.com", "example.com")        # a domain covers its subdomains
    assert H._matches("api.example.com", "*.example.com")
    assert H._matches("example.com", "example.com")
    assert not H._matches("evil.com", "example.com")
    assert not H._matches("notexample.com", "example.com")     # suffix ≠ subdomain
    assert H._in_scope("a.x.com", ["x.com"], [])
    assert not H._in_scope("a.x.com", ["x.com"], ["a.x.com"])  # exclude always wins
    assert not H._in_scope("a.y.com", ["x.com"], [])           # not in the allow-list


async def test_hunt_filters_out_of_scope(monkeypatch):
    from friday.skills import recon, web_recon

    async def fake_find(domain):
        return {"subdomains": ["api.example.com", "legacy.example.com", "blog.other.com"]}

    seen = {}

    async def fake_live(hosts, max_hosts):
        seen["hosts"] = hosts                                  # what passed the scope filter
        return [f"https://{h}" for h in hosts]

    async def fake_nuclei(target, severity="", tags=""):
        return {"findings": []}

    monkeypatch.setattr(recon, "find_subdomains", fake_find)
    monkeypatch.setattr(H, "_live_web_hosts", fake_live)
    monkeypatch.setattr(web_recon, "nuclei_scan", fake_nuclei)

    await H.hunt("example.com", scope="*.example.com", exclude="legacy.example.com")
    assert "api.example.com" in seen["hosts"]
    assert "legacy.example.com" not in seen["hosts"]           # excluded (deny-list)
    assert "blog.other.com" not in seen["hosts"]               # outside the allow-list


async def test_hunt_refuses_out_of_scope_single_target():
    r = await H.hunt("http://evil.com", scope="example.com")
    assert "error" in r and "scope" in r["error"]


async def test_hunt_high_value_profile_targets_payable_tags(monkeypatch):
    from friday.skills import web_recon

    seen = {}

    async def fake_nuclei(target, severity="", tags=""):
        seen["tags"] = tags
        return {"findings": []}

    monkeypatch.setattr(H, "_url_reachable", _reachable)
    monkeypatch.setattr(web_recon, "nuclei_scan", fake_nuclei)

    await H.hunt("http://x.com", profile="high-value")               # default goes for money
    assert "takeover" in seen["tags"] and "default-login" in seen["tags"]
    await H.hunt("http://x.com", profile="high-value", tags="cve")   # explicit tags override
    assert seen["tags"] == "cve"
    await H.hunt("http://x.com", profile="all")                      # 'all' = no tag filter
    assert seen["tags"] == ""


async def test_hunt_surfaces_enum_failure_not_silent(monkeypatch):
    from friday.skills import recon, web_recon

    async def failed_enum(domain):
        return {"error": "'subfinder' is not installed"}

    async def fake_live(hosts, max_hosts):
        return []

    async def fake_nuclei(target, severity="", tags=""):
        return {"findings": []}

    monkeypatch.setattr(recon, "find_subdomains", failed_enum)
    monkeypatch.setattr(H, "_live_web_hosts", fake_live)
    monkeypatch.setattr(web_recon, "nuclei_scan", fake_nuclei)

    res = await H.hunt("example.com")                    # bare domain → enum, which fails
    assert any("enumeration failed" in w for w in res["warnings"])   # not silently '0 assets'
    assert "INCOMPLETE" in res["report"]                 # the report flags it


async def test_hunt_surfaces_scan_timeout_not_clean(monkeypatch):
    from friday.skills import web_recon

    async def timed_out(target, severity="", tags=""):
        return {"findings": [], "note": "scan TIMED OUT — partial", "incomplete": True}

    monkeypatch.setattr(H, "_url_reachable", _reachable)
    monkeypatch.setattr(web_recon, "nuclei_scan", timed_out)

    res = await H.hunt("http://x.com")
    assert any("TIMED OUT" in w for w in res["warnings"])  # 0 findings ≠ clean
    assert "INCOMPLETE" in res["report"]


async def test_hunt_dead_single_target_fails_fast(monkeypatch):
    async def unreachable(url, timeout=3.0):
        return False

    monkeypatch.setattr(H, "_url_reachable", unreachable)
    res = await H.hunt("http://192.0.2.1")               # never reachable → must not run a scan
    assert "error" in res and "not reachable" in res["error"]


async def test_hunt_content_discovery_surfaces_sensitive_paths(monkeypatch):
    from friday.skills import web_recon

    async def fake_nuclei(target, severity="", tags=""):
        return {"findings": []}

    async def fake_enum(url, mode="dirs", wordlist=""):
        return {"found": [
            {"path": "/.git/config", "status": 200, "size": 92},   # HOT (VCS) → medium
            {"path": "/admin", "status": 200, "size": 500},        # WARM (panel) → low
            {"path": "/about", "status": 200, "size": 1200},       # benign → ignored
        ]}

    monkeypatch.setattr(H, "_url_reachable", _reachable)
    monkeypatch.setattr(web_recon, "nuclei_scan", fake_nuclei)
    monkeypatch.setattr(web_recon, "web_enum", fake_enum)

    res = await H.hunt("http://x.com", content=True)
    git = next(f for f in res["findings"] if ".git" in f["matched_at"])
    assert git["severity"] == "medium"                                   # HOT path = submit-worthy
    admin = next(f for f in res["findings"] if "/admin" in f["matched_at"])
    assert admin["severity"] == "low"                                    # WARM = low
    assert not any("/about" in f["matched_at"] for f in res["findings"])  # benign path excluded

    res2 = await H.hunt("http://x.com")                                  # content OFF by default
    assert res2["findings"] == []                                        # no brute-force, no noise


async def test_hunt_content_discovery_graceful_without_wordlist(monkeypatch):
    from friday.skills import web_recon

    async def fake_nuclei(target, severity="", tags=""):
        return {"findings": []}

    async def no_wordlist(url, mode="dirs", wordlist=""):
        return {"error": "no wordlist found — install seclists/dirb"}

    monkeypatch.setattr(H, "_url_reachable", _reachable)
    monkeypatch.setattr(web_recon, "nuclei_scan", fake_nuclei)
    monkeypatch.setattr(web_recon, "web_enum", no_wordlist)

    res = await H.hunt("http://x.com", content=True)                     # must not crash
    assert any("content discovery skipped" in w for w in res["warnings"])


async def test_hunt_active_runs_a_dast_pass(monkeypatch):
    from friday.skills import web_recon

    calls = []

    async def fake_nuclei(target, severity="", tags="", active=False):
        calls.append(active)
        if active:
            return {"findings": [{"id": "ssrf", "name": "SSRF", "severity": "high",
                                  "matched_at": target}]}
        return {"findings": []}

    monkeypatch.setattr(H, "_url_reachable", _reachable)
    monkeypatch.setattr(web_recon, "nuclei_scan", fake_nuclei)

    res = await H.hunt("http://x.com", active=True)
    assert True in calls                                   # a DAST pass actually ran
    assert any(f.get("active") for f in res["findings"])   # active finding is tagged as such


async def test_hunt_active_crawls_and_feeds_coach(monkeypatch):
    from friday.skills import web_recon

    async def fake_nuclei(target, severity="", tags="", active=False):
        return {"findings": []}

    async def fake_crawl(url, depth=2):
        return {"count": 2, "urls": ["http://x.com/api/Cards", "http://x.com/rest/admin"]}

    monkeypatch.setattr(H, "_url_reachable", _reachable)
    monkeypatch.setattr(web_recon, "nuclei_scan", fake_nuclei)
    monkeypatch.setattr(web_recon, "crawl", fake_crawl)

    res = await H.hunt("http://x.com", active=True)
    assert "http://x.com/api/Cards" in res["crawled"]       # crawled the real surface
    assert "/api/Cards" in res["report"]                    # coach worklist covers the endpoint


async def test_hunt_honors_explicit_port_in_liveness(monkeypatch):
    # the live-caught bug: a target on :3000 must be probed on :3000, not 80/443
    seen = {}

    async def fake_open(host, port):
        seen["port"] = port
        raise OSError("refused")  # doesn't matter; we only assert the PORT we probed

    monkeypatch.setattr(H.asyncio, "open_connection", fake_open)
    await H._url_reachable("http://localhost:3000")
    assert seen["port"] == 3000                          # honored the explicit port, not 80/443
