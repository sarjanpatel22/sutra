"""Tests for the Firefox→Sutra cookie bridge.

A synthetic cookies.sqlite (Firefox's schema) stands in for a real profile, so the
conversion to Playwright cookies — domain filtering, sameSite mapping, session vs.
dated expiry, secure/httpOnly — is checked without touching anyone's browser.
"""

import sqlite3

import pytest

from friday.skills import registry
from friday.skills.firefox_login import (
    apply_firefox_login,
    domain_of,
    import_firefox_login,
    read_firefox_cookies,
)


@pytest.fixture()
def fake_profile(tmp_path, monkeypatch):
    db = tmp_path / "cookies.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, path TEXT, "
        "expiry INTEGER, isSecure INTEGER, isHttpOnly INTEGER, sameSite INTEGER)"
    )
    con.executemany(
        "INSERT INTO moz_cookies VALUES (?,?,?,?,?,?,?,?)",
        [
            (".tryhackme.com", "session", "TOK1", "/", 2000000000, 1, 1, 2),
            ("www.tryhackme.com", "csrf", "TOK2", "/", 0, 1, 0, 1),  # session cookie
            ("github.com", "logged_in", "yes", "/", 1999999999, 1, 1, 0),  # None+secure
            ("insecure.test", "x", "y", "/", 0, 0, 0, 0),  # None+insecure → no sameSite
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setenv("FIREFOX_PROFILE_DIR", str(tmp_path))
    return tmp_path


def test_domain_of():
    assert domain_of("https://www.tryhackme.com/room/x") == "tryhackme.com"
    assert domain_of("tryhackme.com") == "tryhackme.com"
    assert domain_of("https://en.wikipedia.org/wiki/Y") == "en.wikipedia.org"


def test_reads_all_cookies(fake_profile):
    cookies = read_firefox_cookies()
    assert len(cookies) == 4
    by_name = {c["name"]: c for c in cookies}
    assert by_name["session"]["value"] == "TOK1"
    assert by_name["session"]["sameSite"] == "Strict" and by_name["session"]["httpOnly"] is True
    assert by_name["session"]["expires"] == 2000000000.0
    assert by_name["csrf"]["sameSite"] == "Lax" and by_name["csrf"]["expires"] == -1  # session
    assert by_name["logged_in"]["sameSite"] == "None"  # 0 + secure → None
    assert "sameSite" not in by_name["x"]  # 0 + insecure → omitted (Playwright would reject)


def test_domain_filter_matches_subdomains(fake_profile):
    cookies = read_firefox_cookies("tryhackme.com")
    assert {c["name"] for c in cookies} == {"session", "csrf"}  # both THM hosts, not github
    assert read_firefox_cookies("github.com")[0]["name"] == "logged_in"
    assert read_firefox_cookies("nonexistent.com") == []


def test_missing_profile_is_empty(monkeypatch):
    monkeypatch.setenv("FIREFOX_PROFILE_DIR", "/no/such/dir")
    assert read_firefox_cookies() == []


async def test_apply_firefox_login_adds_to_context(fake_profile):
    added = []

    class Ctx:
        async def add_cookies(self, cookies):
            added.extend(cookies)

    n = await apply_firefox_login(Ctx(), "tryhackme.com")
    assert n == 2 and {c["name"] for c in added} == {"session", "csrf"}


async def test_apply_retries_without_samesite_on_error(fake_profile):
    calls = []

    class PickyCtx:
        async def add_cookies(self, cookies):
            calls.append([dict(c) for c in cookies])
            if len(calls) == 1:
                raise ValueError("sameSite=None requires secure")  # first attempt rejected

    n = await apply_firefox_login(PickyCtx(), "")
    assert n == 4 and len(calls) == 2
    assert all("sameSite" not in c for c in calls[1])  # retry stripped sameSite


def test_skill_is_registered_and_auto():
    registry.autodiscover()
    assert registry.get("import_firefox_login").auto_confirm is True


async def test_import_skill_happy_path(fake_profile, monkeypatch):
    class Ctx:
        async def add_cookies(self, cookies):
            pass

    class Page:
        context = Ctx()

    async def fake_get_page():
        return Page()

    monkeypatch.setattr("friday.skills.browser_session.get_page", fake_get_page)
    out = await import_firefox_login("tryhackme.com")
    assert out["status"] == "ok" and out["imported"] == 2


async def test_import_skill_reports_nothing(monkeypatch):
    monkeypatch.setenv("FIREFOX_PROFILE_DIR", "/no/such/dir")

    class Page:
        context = type("C", (), {"add_cookies": None})()

    async def fake_get_page():
        return Page()

    monkeypatch.setattr("friday.skills.browser_session.get_page", fake_get_page)
    out = await import_firefox_login("whatever.com")
    assert out["status"] == "nothing imported"


def test_no_cookie_values_in_skill_output(fake_profile, monkeypatch):
    """The skill must never echo a token value back into the model/logs."""

    async def run():
        class Ctx:
            async def add_cookies(self, cookies):
                pass

        class Page:
            context = Ctx()

        async def fake_get_page():
            return Page()

        monkeypatch.setattr("friday.skills.browser_session.get_page", fake_get_page)
        return await import_firefox_login()

    import asyncio

    out = asyncio.run(run())
    assert "TOK1" not in str(out) and "TOK2" not in str(out)  # counts only, no values
