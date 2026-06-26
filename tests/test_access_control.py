"""Tests for the access-control / IDOR skill (friday/skills/access_control.py).

Driven against a real LOCAL http server (hermetic, no network) with three endpoints:
  /idor   — any session cookie returns account A's data (broken access control)
  /safe   — each session gets its OWN data; no cookie is denied (correct)
  /public — everyone, incl. anon, gets the same page (public; not IDOR)
"""

from __future__ import annotations

import http.server
import threading

from friday.skills import access_control

_A_DATA = "PRIVATE PROFILE OF ALICE ssn=111 balance=9000 " * 20
_B_DATA = "ZZZZ COMPLETELY DIFFERENT OWN-ACCOUNT ZULU CONTENT " * 20
_PUBLIC = "WELCOME TO THE PUBLIC HOMEPAGE NOTHING SECRET HERE " * 20


def _serve():
    class _H(http.server.BaseHTTPRequestHandler):
        def _send(self, code: int, body: str):
            self.send_response_only(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):  # noqa: N802
            cookie = self.headers.get("Cookie")
            if self.path == "/idor":  # any cookie -> A's data (broken access control)
                self._send(200, _A_DATA) if cookie else self._send(403, "forbidden")
            elif self.path == "/safe":  # each user gets their OWN data; anon denied
                if not cookie:
                    self._send(403, "forbidden")
                elif "session=A" in cookie:
                    self._send(200, _A_DATA)
                else:
                    self._send(200, _B_DATA)
            elif self.path == "/public":  # same for everyone, incl. anon
                self._send(200, _PUBLIC)
            else:
                self._send(404, "nope")

        def log_message(self, *a):  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def _check(path: str, **kw):
    srv, port = _serve()
    try:
        return await access_control.idor_check(f"http://127.0.0.1:{port}{path}", **kw)
    finally:
        srv.shutdown()


# --- detection ---------------------------------------------------------------------


async def test_idor_detected_cross_account():
    out = await _check("/idor", auth_a="session=A", auth_b="session=B")
    assert out["vulnerable"] is True
    ids = [f["id"] for f in out["findings"]]
    assert "idor-cross-account-access" in ids  # B read A's resource
    b = next(t for t in out["tests"] if t["account"] == "B")
    assert b["vulnerable"] is True and b["similarity"] >= 0.95
    anon = next(t for t in out["tests"] if t["account"] == "anon")
    assert anon["vulnerable"] is False and "403" in anon["verdict"]  # anon correctly denied


async def test_safe_endpoint_not_flagged():
    out = await _check("/safe", auth_a="session=A", auth_b="session=B")
    assert out["vulnerable"] is False and out["findings"] == []
    b = next(t for t in out["tests"] if t["account"] == "B")
    assert b["vulnerable"] is False and b["similarity"] < 0.95  # B got its OWN (different) data


async def test_public_resource_suppresses_b_but_flags_anon():
    out = await _check("/public", auth_a="session=A", auth_b="session=B")
    ids = [f["id"] for f in out["findings"]]
    assert "idor-unauthenticated-access" in ids  # anon access flagged
    assert "idor-cross-account-access" not in ids  # B suppressed: it's public
    b = next(t for t in out["tests"] if t["account"] == "B")
    assert b["vulnerable"] is False and "public" in b["verdict"].lower()


async def test_anon_only_when_no_auth_b():
    out = await _check("/idor", auth_a="session=A")  # no auth_b → only anon tested
    assert not any(t["account"] == "B" for t in out["tests"])
    assert out["vulnerable"] is False  # anon is denied on /idor


# --- guards ------------------------------------------------------------------------


async def test_requires_auth_a():
    out = await access_control.idor_check("http://x/idor", auth_a="")
    assert "error" in out and "auth_a" in out["error"]


async def test_no_url():
    assert "error" in await access_control.idor_check("", auth_a="session=A")


async def test_nothing_to_compare():
    out = await access_control.idor_check("http://x", auth_a="session=A", test_anon=False)
    assert "error" in out and "compare" in out["error"]


async def test_unreachable_target_is_graceful():
    out = await access_control.idor_check("http://127.0.0.1:1/idor", auth_a="session=A")
    assert "error" in out  # account A unreachable, no raise


async def test_parse_auth_header_vs_cookie():
    assert access_control._parse_auth("Authorization: Bearer xyz") == {
        "Authorization": "Bearer xyz"
    }
    assert access_control._parse_auth("session=abc") == {"Cookie": "session=abc"}
    assert access_control._parse_auth("") == {}


def test_idor_check_is_gated():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("idor_check").auto_confirm is False  # active, sends auth → gated
