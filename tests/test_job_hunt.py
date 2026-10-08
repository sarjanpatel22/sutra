"""Job-hunt skills: the deterministic TOOLS layer of the apply flow.

All tests are hermetic — no network (postings are passed as text) and the on-disk
tracker is redirected to a tmp file — so they prove the parse/score/track logic and
the verify-before-claim verifiers without touching the real store or the web.
"""
import pytest

from friday.core import postcheck
from friday.skills import job_hunt

RESUME = (
    "Sarjan Patel. Python and SQL developer. Built PowerBI dashboards and API "
    "integrations. Cybersecurity: SIEM, vulnerability assessment, Microsoft 365. "
    "Strong data entry and records management, business analysis."
)

POSTING = (
    "Business Analyst. We need Python, SQL and PowerBI. Requirements gathering and "
    "data entry are essential. Kubernetes and Rust experience is a bonus."
)


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(job_hunt, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(job_hunt, "_APPS_FILE", tmp_path / "job_applications.json")


# --- parse_job_posting ------------------------------------------------------
async def test_parse_plain_text_extracts_keywords():
    r = await job_hunt.parse_job_posting(POSTING)
    assert "error" not in r
    assert "python" in r["keywords"] and "sql" in r["keywords"]
    assert r["text"].startswith("Business Analyst")


async def test_parse_strips_html():
    r = await job_hunt.parse_job_posting("<h1>Dev</h1><p>We use <b>Python</b> &amp; SQL</p>")
    assert "<" not in r["text"] and "python" in r["keywords"]
    assert "&" in r["text"]  # entity unescaped, not left as &amp;


async def test_parse_empty_is_error():
    assert "error" in await job_hunt.parse_job_posting("   ")


# --- load_resume ------------------------------------------------------------
async def test_load_resume_from_path(tmp_path):
    f = tmp_path / "r.txt"
    f.write_text(RESUME)
    r = await job_hunt.load_resume(path=str(f))
    assert r["text"] == RESUME and "python" in r["skills_detected"]


async def test_load_resume_missing_is_error(tmp_path):
    assert "error" in await job_hunt.load_resume(path=str(tmp_path / "nope.html"))


async def test_load_resume_honours_env(tmp_path, monkeypatch):
    f = tmp_path / "env_resume.txt"
    f.write_text(RESUME)
    monkeypatch.setenv("FRIDAY_RESUME", str(f))
    r = await job_hunt.load_resume()  # blank path -> env
    assert r["source"] == str(f)


# --- fit_check --------------------------------------------------------------
async def test_fit_check_is_honest_overlap():
    r = await job_hunt.fit_check(POSTING, RESUME)
    assert "python" in r["matched"] and "sql" in r["matched"]
    # genuinely-absent requirements must show up as gaps, not be invented away
    assert "kubernetes" in r["missing"] and "rust" in r["missing"]
    assert 0 < r["score"] < 100
    assert r["score"] == round(100 * len(r["matched"]) / r["considered"])


async def test_fit_check_loads_resume_when_blank(tmp_path, monkeypatch):
    f = tmp_path / "r.txt"
    f.write_text(RESUME)
    monkeypatch.setenv("FRIDAY_RESUME", str(f))
    r = await job_hunt.fit_check(POSTING)  # no resume_text -> loads env resume
    assert "python" in r["matched"]


async def test_fit_check_needs_a_posting():
    assert "error" in await job_hunt.fit_check("", RESUME)


# --- precision: company name and generic filler must not pose as skills -------
NOISY_POSTING = (
    "Administrative Assistant\n"
    "Company: Northern Tech Solutions\n"
    "We are seeking an organized assistant with familiarity handling confidential "
    "information. Responsibilities include data entry and records management. Python "
    "and SQL are a bonus."
)


def test_company_tokens_excluded_from_posting_keywords():
    company = job_hunt._words("Northern Tech Solutions")
    kws = job_hunt._posting_keywords(NOISY_POSTING, exclude=company)
    assert "northern" not in kws and "solutions" not in kws and "tech" not in kws
    assert "python" in kws and "sql" in kws  # real skills survive


def test_generic_filler_is_not_scored():
    kws = job_hunt._posting_keywords(NOISY_POSTING)
    for junk in ("familiarity", "handle", "handling", "organized", "information", "bonus"):
        assert junk not in kws


async def test_fit_check_auto_strips_company_from_a_company_line():
    r = await job_hunt.fit_check(NOISY_POSTING, RESUME)
    pool = set(r["matched"]) | set(r["missing"])
    # the company name must not be scored as a skill (neither matched nor missing)
    assert not ({"northern", "solutions", "tech"} & pool)
    assert "python" in r["matched"] and "sql" in r["matched"]


async def test_fit_check_explicit_exclude_arg():
    # a caller (e.g. the CLI) can pass the company name even without a Company: line
    posting = "Analyst at Acme. Needs Python, SQL, and Acme-specific data entry."
    r = await job_hunt.fit_check(posting, RESUME, exclude="Acme")
    assert not ({"acme"} & (set(r["matched"]) | set(r["missing"])))


# --- tracker: save / list / update -----------------------------------------
async def test_save_list_update_roundtrip():
    saved = await job_hunt.save_application("Acme", "Analyst", url="http://x", fit_score=72)
    assert saved["status"] == "ok"
    app_id = saved["application"]["id"]

    listed = await job_hunt.list_applications()
    assert listed["count"] == 1 and listed["applications"][0]["id"] == app_id

    upd = await job_hunt.update_application(app_id, status="applied", notes="sent today")
    assert upd["application"]["status"] == "applied" and upd["application"]["notes"] == "sent today"

    only_applied = await job_hunt.list_applications(status="applied")
    assert only_applied["count"] == 1
    assert (await job_hunt.list_applications(status="drafted"))["count"] == 0


async def test_ids_increment_and_persist():
    a = await job_hunt.save_application("A", "r1")
    b = await job_hunt.save_application("B", "r2")
    assert b["application"]["id"] == a["application"]["id"] + 1
    assert b["total_tracked"] == 2


async def test_save_rejects_bad_status():
    assert "error" in await job_hunt.save_application("A", "r", status="bogus")


async def test_update_unknown_id_is_error():
    r = await job_hunt.update_application(999, status="applied")
    assert "error" in r and r["known_ids"] == []


# --- verify-before-claim verifiers -----------------------------------------
async def test_save_verifier_confirms_persisted():
    saved = await job_hunt.save_application("Acme", "Analyst")
    ok, _ = postcheck.verify("save_application", {}, saved)
    assert ok is True


def test_save_verifier_catches_phantom():
    # a result claiming an id that was never written must fail verification
    phantom = {"status": "ok", "application": {"id": 424242}}
    ok, note = postcheck.verify("save_application", {}, phantom)
    assert ok is False and "not in the store" in note


async def test_update_verifier_checks_on_disk_status():
    saved = await job_hunt.save_application("Acme", "Analyst")
    app_id = saved["application"]["id"]
    upd = await job_hunt.update_application(app_id, status="interview")
    ok, _ = postcheck.verify("update_application", {"app_id": app_id, "status": "interview"}, upd)
    assert ok is True
    # a mismatched claim fails
    bad = postcheck.verify("update_application", {"app_id": app_id, "status": "offer"}, upd)
    assert bad[0] is False


def test_verifiers_are_registered():
    assert "save_application" in postcheck._VERIFIERS
    assert "update_application" in postcheck._VERIFIERS
