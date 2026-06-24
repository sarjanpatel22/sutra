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
    findings = [_finding(severity="low", name="Low one", id="low-tmpl"),
                _finding(severity="critical", name="Crit one", id="crit-tmpl")]
    r = bounty.draft_bounty_report("t", findings)
    assert r.index("Crit one") < r.index("Low one")       # critical before low


def test_report_dedups_same_finding_across_hosts():
    f1 = _finding(matched_at="http://a.x.com/metrics")
    f2 = _finding(matched_at="http://b.x.com/metrics")    # same template id, different host
    r = bounty.draft_bounty_report("x.com", [f1, f2])
    assert r.count("## [MEDIUM] Prometheus Metrics") == 1  # one section, not two
    assert "http://a.x.com/metrics" in r and "http://b.x.com/metrics" in r  # both hosts listed
    assert "1 submit-worthy" in r                          # deduped to one


def test_report_separates_signal_from_noise():
    med = _finding(severity="medium", name="Real exposure", id="exp")
    info = _finding(severity="info", name="Tech detect", id="tech")
    r = bounty.draft_bounty_report("prog", [med, info])
    assert "Submit-worthy findings" in r and "Informational" in r
    assert r.index("Real exposure") < r.index("## Informational")  # signal above the noise section
    assert "Tech detect" in r                              # noise condensed, not a full section
    assert "1 submit-worthy, 1 informational" in r


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
