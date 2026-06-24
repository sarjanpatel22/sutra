"""Tests for the bug-bounty report drafter — deterministic, grounded entirely in real findings."""

from typer.testing import CliRunner

import friday.cli as cli
from friday.core import bounty


def _finding(**kw):
    base = {
        "id": "prometheus-metrics", "name": "Prometheus Metrics", "severity": "medium",
        "matched_at": "http://t/metrics", "tags": ["exposure"],
        "description": "Prometheus metrics page detected.", "cvss": 5.3, "cwe": ["CWE-200"],
        "reference": ["https://hackerone.com/reports/1026196"],
        "curl": "curl -X GET http://t/metrics",
    }
    base.update(kw)
    return base


def test_draft_report_grounds_every_field_in_real_data():
    r = bounty.draft_bounty_report("http://t", [_finding()], program="Acme")
    assert "Prometheus Metrics" in r and "[MEDIUM]" in r
    assert "http://t/metrics" in r                        # affected URL (real)
    assert "curl -X GET http://t/metrics" in r            # reproduction (real curl)
    assert "CVSS 5.3" in r and "CWE-200" in r             # classification (real)
    assert "hackerone.com/reports/1026196" in r           # references (real)
    assert "VERIFY each finding" in r                     # the submit-safely warning
    assert "program: Acme" in r


def test_draft_report_sorts_worst_first():
    findings = [_finding(severity="low", name="Low one"),
                _finding(severity="critical", name="Crit one")]
    r = bounty.draft_bounty_report("t", findings)
    assert r.index("Crit one") < r.index("Low one")       # critical before low


def test_draft_report_empty():
    r = bounty.draft_bounty_report("t", [])
    assert "No findings to report" in r and "**Findings:** 0" in r


def test_draft_report_synthesizes_repro_when_no_curl():
    r = bounty.draft_bounty_report("t", [_finding(curl="")])
    assert "curl -i 'http://t/metrics'" in r               # repro built from the real URL


def test_cli_bounty_scans_then_drafts_and_saves(monkeypatch, tmp_path):
    from friday.skills import web_recon

    async def fake_scan(target, severity="", tags=""):
        return {"target": target, "count": 1, "findings": [_finding()]}

    monkeypatch.setattr(web_recon, "nuclei_scan", fake_scan)
    out_file = tmp_path / "report.md"
    r = CliRunner().invoke(cli.app, ["bounty", "http://t", "--out", str(out_file)])
    assert r.exit_code == 0, r.output
    assert out_file.exists()
    text = out_file.read_text()
    assert "Prometheus Metrics" in text and "curl -X GET http://t/metrics" in text
