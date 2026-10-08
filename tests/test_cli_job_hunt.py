"""`sutra apply` and `sutra jobs` — the job-hunt CLI orchestration.

Hermetic: the model call is stubbed, the resume comes from a tmp file, and the
tracker store is redirected to tmp, so these prove the parse -> fit -> draft ->
(optional) track wiring without the network, a real model, or the real store.
"""
from types import SimpleNamespace

from typer.testing import CliRunner

import friday.cli as cli
from friday.core import router
from friday.skills import job_hunt

runner = CliRunner()

RESUME = (
    "Sarjan Patel. Python, SQL, PowerBI. Administrative support: data entry, "
    "records and document management, quotes and invoices, client communication."
)

POSTING = (
    "Administrative Assistant\n"
    "Company: Northern Tech Solutions\n"
    "Needs data entry, records management, invoices, Python and SQL a plus."
)


async def _fake_complete(messages, tier=None, **kw):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="CANNEDLETTER grounded in the resume only."))])


def _wire(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "complete", _fake_complete)
    monkeypatch.setattr(job_hunt, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(job_hunt, "_APPS_FILE", tmp_path / "apps.json")
    resume = tmp_path / "resume.txt"
    resume.write_text(RESUME)
    monkeypatch.setenv("FRIDAY_RESUME", str(resume))


def test_apply_drafts_grounded_letter_without_saving(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    r = runner.invoke(cli.app, ["apply", POSTING, "--no-save", "--tier", "medium"])
    assert r.exit_code == 0, r.output
    assert "Fit" in r.output and "%" in r.output        # fit panel rendered
    assert "CANNEDLETTER" in r.output                   # the drafted letter shown
    assert "does not send" in r.output                  # the never-sends reassurance
    assert not (tmp_path / "apps.json").exists()        # --no-save tracked nothing


def test_apply_requires_a_posting(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    r = runner.invoke(cli.app, ["apply", "", "--no-save"])
    assert r.exit_code == 1
    assert "posting" in r.output.lower()


def test_apply_errors_when_resume_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "complete", _fake_complete)
    monkeypatch.setenv("FRIDAY_RESUME", str(tmp_path / "nope.txt"))
    r = runner.invoke(cli.app, ["apply", POSTING, "--no-save"])
    assert r.exit_code == 1
    assert "resume" in r.output.lower()


def test_jobs_empty_then_lists(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    empty = runner.invoke(cli.app, ["jobs"])
    assert empty.exit_code == 0 and "No tracked applications" in empty.output

    import asyncio
    asyncio.run(job_hunt.save_application("Northern Tech", "Analyst", fit_score=72))
    listed = runner.invoke(cli.app, ["jobs"])
    assert listed.exit_code == 0
    assert "Northern Tech" in listed.output and "Analyst" in listed.output
    assert "72%" in listed.output


def test_jobs_filters_by_status(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    import asyncio
    asyncio.run(job_hunt.save_application("A", "r1", status="applied"))
    asyncio.run(job_hunt.save_application("B", "r2", status="drafted"))
    r = runner.invoke(cli.app, ["jobs", "--status", "applied"])
    assert r.exit_code == 0 and "r1" in r.output and "r2" not in r.output
