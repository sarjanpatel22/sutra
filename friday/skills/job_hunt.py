"""Job-hunt skills — the deterministic half of "my friend runs my job search".

Same reliability-safe split as the rest of Sutra: these TOOLS do the deterministic
work (fetch & parse a posting, load the REAL resume, score the literal skills
overlap, track applications on disk) while the MODEL writes the tailored letter
grounded ONLY in the resume text these tools return, and the HUMAN approves before
anything is sent. Nothing here fabricates experience and nothing here submits an
application — sending stays a human action on purpose.
"""
from __future__ import annotations

import html
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from friday.core import postcheck
from friday.skills.registry import skill

_DATA_DIR = Path.home() / ".local" / "share" / "friday"
_APPS_FILE = _DATA_DIR / "job_applications.json"
_DEFAULT_RESUME = Path.home() / "resume" / "Sarjan_Patel_Resume.html"

# a status is one of these — kept small so the tracker stays legible
_STATUSES = ("drafted", "applied", "interview", "offer", "rejected", "archived")

# words too generic to signal a real skill match
_STOP = {
    "the", "and", "for", "with", "you", "your", "our", "are", "will", "have", "has",
    "was", "this", "that", "from", "they", "their", "them", "who", "what", "which",
    "all", "any", "can", "should", "must", "able", "work", "working", "team", "role",
    "job", "position", "experience", "years", "year", "including", "etc", "such",
    "within", "across", "strong", "excellent", "good", "ability", "skills", "skill",
    "knowledge", "plus", "responsibilities", "requirements", "required", "preferred",
    "qualifications", "candidate", "candidates", "company", "benefits", "apply", "per",
    "applicants", "day", "office", "new", "using", "use", "used", "help", "support",
    "ensure", "provide", "high", "about", "into", "out", "not", "but", "its", "we",
    "us", "an", "to", "of", "in", "on", "at", "as", "or", "be", "is", "by", "will",
    "join", "looking", "seeking", "opportunity", "please", "email", "resume", "us",
    # generic recruiting filler that isn't a skill — kept out of matched/missing so a
    # fit check reports real strengths and real gaps, not noise
    "familiarity", "handle", "handling", "information", "organized", "detail", "oriented",
    "environment", "duties", "tasks", "various", "related", "assist", "assisting",
    "responsible", "essential", "bonus", "fast", "paced", "flexible", "friendly",
}

# multi-word skills worth matching as one unit (checked as substrings, lowercase)
_PHRASES = (
    "business analysis", "data entry", "power bi", "powerbi", "machine learning",
    "project management", "customer service", "account management", "front office",
    "accounts receivable", "accounts payable", "microsoft office", "microsoft 365",
    "active directory", "incident response", "vulnerability assessment", "data entry",
    "record keeping", "data management", "requirements gathering", "process mapping",
    "attention to detail", "problem solving", "quickbooks", "google workspace",
)


# --- text helpers (pure, no network) ---------------------------------------
def _strip_html(raw: str) -> str:
    """HTML -> readable plain text, dependency-free."""
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?is)</(p|div|li|h[1-6]|tr|section)>", "\n", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _words(text: str) -> set[str]:
    """Meaningful word-level terms in a block of text."""
    out: set[str] = set()
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+#.\-]{2,}", text.lower()):
        w = w.strip(".-")
        if len(w) >= 3 and w not in _STOP:
            out.add(w)
    return out


def _posting_keywords(text: str, limit: int = 30, exclude: set[str] | None = None) -> list[str]:
    """The terms a posting emphasises most — top words by frequency, plus any
    multi-word skill phrases it contains. This is what we score the resume against.

    exclude: tokens to drop (e.g. the company name), so they never pose as skills."""
    skip = _STOP | (exclude or set())
    low = text.lower()
    freq: dict[str, int] = {}
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+#.\-]{2,}", low):
        w = w.strip(".-")
        if len(w) >= 3 and w not in skip:
            freq[w] = freq.get(w, 0) + 1
    ranked = sorted(freq, key=lambda k: (-freq[k], k))[:limit]
    for ph in _PHRASES:
        if ph in low and ph not in ranked and ph not in (exclude or set()):
            ranked.append(ph)
    return ranked


def _present(term: str, resume_words: set[str], resume_low: str) -> bool:
    """Phrase terms match as substrings; single words match at word level."""
    return (term in resume_low) if " " in term else (term in resume_words)


# --- store helpers ----------------------------------------------------------
def _load_apps() -> list[dict[str, Any]]:
    if not _APPS_FILE.exists():
        return []
    try:
        data = json.loads(_APPS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write_apps(apps: list[dict[str, Any]]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _APPS_FILE.write_text(json.dumps(apps, indent=2))


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


# --- skills -----------------------------------------------------------------
@skill(
    name="parse_job_posting",
    description=(
        "Read a job/role posting from a URL or pasted text and return its title, "
        "company, cleaned description text, and the key skill terms it emphasises. "
        "First step when the user drops a job link or posting to apply to."
    ),
    auto_confirm=True,
    timeout_s=20,
)
async def parse_job_posting(source: str) -> dict[str, Any]:
    """Parse a posting.

    source: a URL (http...) to fetch, or the raw pasted posting text.
    """
    source = (source or "").strip()
    if not source:
        return {"error": "no source given — pass a job URL or the posting text"}
    url = ""
    if re.match(r"(?i)^https?://", source):
        url = source
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                raw = r.text
        except Exception as e:  # noqa: BLE001 — hand the error back, don't raise
            return {"error": f"could not fetch posting: {e}", "url": url}
        text = _strip_html(raw)
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
        title = html.unescape(m.group(1)).strip() if m else ""
    else:
        text = _strip_html(source) if "<" in source and ">" in source else source.strip()
        title = text.splitlines()[0][:120] if text else ""

    if not text:
        return {"error": "posting had no readable text", "url": url}
    # best-effort company: a line like "Company: X" or "at X" in the title
    company = ""
    cm = re.search(r"(?im)^\s*company\s*[:\-]\s*(.+)$", text)
    if cm:
        company = cm.group(1).strip()[:80]
    elif title:
        tm = re.search(r"(?i)\bat\s+([A-Z][\w&.\- ]{2,40})", title)
        if tm:
            company = tm.group(1).strip()

    return {
        "title": title,
        "company": company,
        "url": url,
        "text": text[:12000],
        "keywords": _posting_keywords(text, exclude=_words(company)),
        "note": "This is the external posting; verify company/title before applying.",
    }


@skill(
    name="load_resume",
    description=(
        "Load the user's real resume as plain text — the ground truth to tailor "
        "applications against. Use before writing any cover letter or fit summary so "
        "nothing is invented: only what is actually in the resume may be claimed."
    ),
    auto_confirm=True,
    timeout_s=10,
)
async def load_resume(path: str = "") -> dict[str, Any]:
    """Load the resume text.

    path: optional resume file (.html/.txt/.md/.pdf); blank uses FRIDAY_RESUME or
          the default resume on disk.
    """
    import os

    chosen = path or os.getenv("FRIDAY_RESUME", "") or str(_DEFAULT_RESUME)
    p = Path(chosen).expanduser()
    if not p.exists():
        return {"error": f"resume not found at {p}", "hint": "pass path= or set FRIDAY_RESUME"}
    suffix = p.suffix.lower()
    try:
        if suffix == ".pdf":
            import shutil
            import subprocess

            if not shutil.which("pdftotext"):
                return {"error": "pdftotext not installed — give an .html/.txt resume instead"}
            out = subprocess.run(
                ["pdftotext", "-layout", str(p), "-"],
                capture_output=True, text=True, timeout=10,
            )
            text = out.stdout
        else:
            raw = p.read_text(errors="replace")
            text = _strip_html(raw) if suffix in (".html", ".htm") else raw
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not read resume: {e}", "path": str(p)}
    text = text.strip()
    if not text:
        return {"error": f"resume at {p} is empty"}
    return {"source": str(p), "text": text, "skills_detected": sorted(_words(text))[:60]}


@skill(
    name="fit_check",
    description=(
        "Score how well the user's resume matches a posting — a literal, honest "
        "skills-overlap number (not a guess) plus the exact matched and missing "
        "terms, so a cover letter can lean on real strengths and address real gaps."
    ),
    auto_confirm=True,
    timeout_s=10,
)
async def fit_check(posting_text: str, resume_text: str = "", exclude: str = "") -> dict[str, Any]:
    """Deterministic fit score.

    posting_text: the posting description (or its key text).
    resume_text: resume plain text; blank loads the default resume.
    exclude: extra terms to ignore (e.g. the company name) so they aren't scored
             as skills; a "Company: X" line in the posting is stripped automatically.
    """
    if not resume_text:
        loaded = await load_resume()
        if "error" in loaded:
            return {"error": f"no resume to compare against: {loaded['error']}"}
        resume_text = loaded["text"]
    skip = _words(exclude)
    cm = re.search(r"(?im)^\s*company\s*[:\-]\s*(.+)$", posting_text)
    if cm:
        skip |= _words(cm.group(1))
    keywords = _posting_keywords(posting_text, exclude=skip)
    if not keywords:
        return {"error": "no skill terms found in the posting text"}
    resume_low = resume_text.lower()
    resume_words = _words(resume_text)
    matched = [k for k in keywords if _present(k, resume_words, resume_low)]
    missing = [k for k in keywords if k not in matched]
    score = round(100 * len(matched) / len(keywords))
    return {
        "score": score,
        "matched": matched,
        "missing": missing,
        "considered": len(keywords),
        "note": (
            f"{len(matched)} of {len(keywords)} key posting terms appear in the resume. "
            "This is literal overlap, not a hiring prediction — read matched/missing."
        ),
    }


@skill(
    name="save_application",
    description=(
        "Track a job application on disk after the user approves it: company, role, "
        "link, status, and fit score. Does NOT send anything — only records it so the "
        "search can be followed over time."
    ),
    auto_confirm=True,
    timeout_s=10,
)
async def save_application(
    company: str,
    role: str,
    url: str = "",
    status: str = "drafted",
    fit_score: int | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Record a tracked application.

    company/role: who and what.
    url: the posting link, if any.
    status: one of drafted, applied, interview, offer, rejected, archived.
    fit_score: the fit_check score, if known.
    notes: anything to remember (deadline, contact, tailoring done).
    """
    if status not in _STATUSES:
        return {"error": f"status must be one of {list(_STATUSES)}", "got": status}
    apps = _load_apps()
    rec = {
        "id": max((a.get("id", 0) for a in apps), default=0) + 1,
        "company": company,
        "role": role,
        "url": url,
        "status": status,
        "fit_score": fit_score,
        "notes": notes,
        "created": _now(),
        "updated": _now(),
    }
    apps.append(rec)
    _write_apps(apps)
    return {"status": "ok", "application": rec, "total_tracked": len(apps)}


@skill(
    name="list_applications",
    description=(
        "List tracked job applications and their status (drafted / applied / "
        "interview / offer / rejected / archived) — the user's pipeline at a glance."
    ),
    auto_confirm=True,
    timeout_s=10,
)
async def list_applications(status: str = "") -> dict[str, Any]:
    """List tracked applications.

    status: optional filter; blank returns all.
    """
    apps = _load_apps()
    if status:
        apps = [a for a in apps if a.get("status") == status]
    return {"count": len(apps), "applications": apps}


@skill(
    name="update_application",
    description=(
        "Update a tracked application's status or notes by its id — e.g. move it to "
        "'applied' once sent, or 'interview' when they reply."
    ),
    auto_confirm=True,
    timeout_s=10,
)
async def update_application(app_id: int, status: str = "", notes: str = "") -> dict[str, Any]:
    """Update a tracked application.

    app_id: the id from save_application / list_applications.
    status: new status (drafted/applied/interview/offer/rejected/archived), or blank.
    notes: replacement notes, or blank to leave unchanged.
    """
    if status and status not in _STATUSES:
        return {"error": f"status must be one of {list(_STATUSES)}", "got": status}
    apps = _load_apps()
    for a in apps:
        if a.get("id") == app_id:
            if status:
                a["status"] = status
            if notes:
                a["notes"] = notes
            a["updated"] = _now()
            _write_apps(apps)
            return {"status": "ok", "application": a}
    return {"error": f"no application with id {app_id}", "known_ids": [a.get("id") for a in apps]}


# --- verify-before-claim: the write skills must prove the record really landed ---
def _v_save_application(args: dict[str, Any], result: Any) -> tuple[bool, str] | None:
    if not isinstance(result, dict) or "application" not in result:
        return None
    want = result["application"].get("id")
    if any(a.get("id") == want for a in _load_apps()):
        return True, f"application {want} persisted"
    return False, f"save reported ok but application {want} is not in the store"


def _v_update_application(args: dict[str, Any], result: Any) -> tuple[bool, str] | None:
    if not isinstance(result, dict) or "application" not in result:
        return None
    want = args.get("status")
    if not want:
        return None
    rec = next((a for a in _load_apps() if a.get("id") == args.get("app_id")), None)
    if rec is None:
        return False, "update reported ok but the application is gone from the store"
    if rec.get("status") == want:
        return True, f"status is {want} on disk"
    return False, f"update reported ok but on-disk status is {rec.get('status')}, not {want}"


postcheck.register("save_application", _v_save_application)
postcheck.register("update_application", _v_update_application)
