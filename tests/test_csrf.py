"""Tests for the CSRF skill (friday/skills/csrf.py) — hermetic server."""

from __future__ import annotations

import http.server
import threading

from friday.skills import csrf

_PAGE = """
<html><body>
  <form method="POST" action="/transfer">
    <input type="text" name="amount">
  </form>
  <form method="post" action="/profile">
    <input type="hidden" name="csrf_token" value="abc">
    <input type="text" name="bio">
  </form>
  <form method="GET" action="/search">
    <input type="text" name="q">
  </form>
</body></html>
"""


def _serve(page=_PAGE, cookies=None):
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            data = page.encode()
            self.send_response_only(200)
            self.send_header("Content-Length", str(len(data)))
            for c in (cookies or []):
                self.send_header("Set-Cookie", c)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def _check(page=_PAGE, cookies=None):
    srv = _serve(page, cookies)
    try:
        return await csrf.check_csrf(f"http://127.0.0.1:{srv.server_address[1]}/")
    finally:
        srv.shutdown()


def _ids(out):
    return [f["id"] for f in out["findings"]]


async def test_flags_post_form_without_token_only():
    out = await _check()
    assert out["post_forms"] == 2          # two POST forms (GET form ignored)
    assert out["unprotected_forms"] == 1   # only /transfer lacks a token
    missing = [f for f in out["findings"] if f["id"] == "csrf-missing-token"]
    assert len(missing) == 1 and "/transfer" in missing[0]["name"]


async def test_tokened_form_not_flagged():
    out = await _check()
    assert not any("/profile" in f["name"] for f in out["findings"])  # has csrf_token


async def test_samesite_none_insecure_cookie_flagged():
    out = await _check(page="<html>no forms</html>",
                       cookies=["sessionid=abc; SameSite=None; Path=/"])
    assert "cookie-samesite-none-insecure" in _ids(out)


async def test_cookie_without_samesite_flagged_low():
    out = await _check(page="<html>no forms</html>", cookies=["auth=xyz; Path=/"])
    f = next(f for f in out["findings"] if f["id"] == "cookie-no-samesite")
    assert f["severity"] == "low"


async def test_secure_samesite_strict_cookie_not_flagged():
    out = await _check(page="<html>no forms</html>",
                       cookies=["sessionid=abc; SameSite=Strict; Secure; Path=/"])
    assert out["findings"] == []  # properly protected → nothing flagged


async def test_non_session_cookie_ignored():
    out = await _check(page="<html>no forms</html>", cookies=["theme=dark; Path=/"])
    assert out["findings"] == []  # not a session cookie → not CSRF-relevant


async def test_unreachable_is_graceful():
    out = await csrf.check_csrf("http://127.0.0.1:1/")
    assert "error" in out


async def test_no_url():
    assert "error" in await csrf.check_csrf("")


def test_check_csrf_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("check_csrf").auto_confirm is True  # read-only GET → passive
