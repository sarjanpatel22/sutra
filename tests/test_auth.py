"""Tests for the JWT analysis skill (friday/skills/auth.py) — real generated tokens, no network."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from friday.skills import auth


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _make_jwt(header: dict, payload: dict, secret: str | None = None) -> str:
    si = f"{_b64(header)}.{_b64(payload)}"
    if secret is None:  # alg:none / unsigned → empty signature segment
        return si + "."
    sig = (
        base64.urlsafe_b64encode(hmac.new(secret.encode(), si.encode(), hashlib.sha256).digest())
        .rstrip(b"=")
        .decode()
    )
    return f"{si}.{sig}"


def _ids(out) -> list[str]:
    return [f["id"] for f in out["findings"]]


async def test_alg_none_is_critical():
    tok = _make_jwt({"alg": "none", "typ": "JWT"}, {"sub": "1", "exp": time.time() + 999})
    out = await auth.analyze_jwt(tok)
    assert "jwt-alg-none" in _ids(out) and out["forgeable"] is True
    assert out["vulnerable"] is True


async def test_weak_hs256_secret_is_cracked():
    tok = _make_jwt(
        {"alg": "HS256", "typ": "JWT"}, {"role": "user", "exp": time.time() + 999}, secret="secret"
    )
    out = await auth.analyze_jwt(tok)
    assert out["cracked_secret"] == "secret" and out["forgeable"] is True
    assert "jwt-weak-secret" in _ids(out)
    # privilege claim + forgeable → escalates to high
    claim = next(f for f in out["findings"] if f["id"] == "jwt-tamperable-claims")
    assert claim["severity"] == "high"


async def test_strong_hs256_secret_not_cracked():
    tok = _make_jwt(
        {"alg": "HS256", "typ": "JWT"},
        {"role": "user", "exp": time.time() + 999},
        secret="9f3c1aa7e4b24c0e8d2f6b1c5a7e90 d-unguessable",
    )
    out = await auth.analyze_jwt(tok)
    assert out["cracked_secret"] is None
    assert "jwt-weak-secret" not in _ids(out)
    # claim present but NOT forgeable → only a low tamper candidate
    claim = next(f for f in out["findings"] if f["id"] == "jwt-tamperable-claims")
    assert claim["severity"] == "low"


async def test_wordlist_extends_the_crack(tmp_path):
    wl = tmp_path / "secrets.txt"
    wl.write_text("nope1\nhunter2-custom-secret\nnope2\n")
    tok = _make_jwt(
        {"alg": "HS256"}, {"sub": "1", "exp": time.time() + 999}, secret="hunter2-custom-secret"
    )
    out = await auth.analyze_jwt(tok, wordlist=str(wl))
    assert out["cracked_secret"] == "hunter2-custom-secret"


async def test_missing_expiry_flagged():
    tok = _make_jwt({"alg": "HS256"}, {"sub": "1"}, secret="x9_strong_not_in_list_zz")
    assert "jwt-no-expiry" in _ids(await auth.analyze_jwt(tok))


async def test_expired_token_flagged():
    tok = _make_jwt(
        {"alg": "HS256"}, {"sub": "1", "exp": time.time() - 100}, secret="x9_strong_not_in_list_zz"
    )
    assert "jwt-expired" in _ids(await auth.analyze_jwt(tok))


async def test_rs256_alg_confusion_candidate():
    tok = _make_jwt({"alg": "RS256"}, {"sub": "1", "exp": time.time() + 999}, secret="ignored")
    assert "jwt-alg-confusion-candidate" in _ids(await auth.analyze_jwt(tok))


async def test_not_a_jwt():
    assert "error" in await auth.analyze_jwt("just.two")
    assert "error" in await auth.analyze_jwt("")


async def test_garbage_segments():
    assert "error" in await auth.analyze_jwt("@@@.@@@.@@@")


def test_analyze_jwt_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("analyze_jwt").auto_confirm is True  # local analysis, no requests → auto
