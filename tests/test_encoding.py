"""Tests for the encode/decode/hash CTF skills (friday/skills/encoding.py)."""

from __future__ import annotations

import pytest

from friday.skills import encoding

# --- transform_text -----------------------------------------------------------------

@pytest.mark.parametrize(
    ("op", "text", "expected"),
    [
        ("base64_encode", "hello", "aGVsbG8="),
        ("hex_encode", "hi", "6869"),
        ("url_encode", "a b&c", "a%20b%26c"),
        ("rot13", "Hello", "Uryyb"),
    ],
)
async def test_transform_encodes(op, text, expected):
    out = await encoding.transform_text(text, op)
    assert out["result"] == expected
    assert out["operation"] == op


@pytest.mark.parametrize(
    ("op", "text", "expected"),
    [
        ("base64_decode", "aGVsbG8=", "hello"),
        ("hex_decode", "6869", "hi"),
        ("url_decode", "a%20b%26c", "a b&c"),
        ("rot13", "Uryyb", "Hello"),  # rot13 is its own inverse
    ],
)
async def test_transform_decodes(op, text, expected):
    out = await encoding.transform_text(text, op)
    assert out["result"] == expected


async def test_transform_round_trips_base64():
    enc = (await encoding.transform_text("CTF{flag}", "base64_encode"))["result"]
    dec = (await encoding.transform_text(enc, "base64_decode"))["result"]
    assert dec == "CTF{flag}"


async def test_transform_normalizes_operation_name():
    # hyphens / casing / spacing are tolerated so the model isn't punished for "Base64-Encode"
    out = await encoding.transform_text("hi", " Base64-Encode ")
    assert out["result"] == "aGk="


async def test_transform_unknown_operation_errors_gracefully():
    out = await encoding.transform_text("x", "rot47")
    assert "error" in out and "rot47" in out["error"]


async def test_transform_bad_hex_returns_error_not_raise():
    out = await encoding.transform_text("zz", "hex_decode")  # not valid hex
    assert "error" in out


# --- hash_text ----------------------------------------------------------------------

async def test_hash_known_vectors():
    out = await encoding.hash_text("abc")
    assert out["digests"]["md5"] == "900150983cd24fb0d6963f7d28e17f72"
    assert out["digests"]["sha1"] == "a9993e364706816aba3e25717850c26c9cd0d89d"
    assert out["digests"]["sha256"] == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


async def test_hash_single_algorithm():
    out = await encoding.hash_text("abc", "sha256")
    assert set(out["digests"]) == {"sha256"}


async def test_hash_unknown_algorithm_errors():
    out = await encoding.hash_text("abc", "sha999")
    assert "error" in out


# --- identify_hash ------------------------------------------------------------------

async def test_identify_md5_length():
    digest = (await encoding.hash_text("abc", "md5"))["digests"]["md5"]
    out = await encoding.identify_hash(digest)
    assert "MD5" in out["candidates"]


async def test_identify_sha256_length():
    digest = (await encoding.hash_text("abc", "sha256"))["digests"]["sha256"]
    out = await encoding.identify_hash(digest)
    assert "SHA-256" in out["candidates"]


async def test_identify_bcrypt_prefix():
    out = await encoding.identify_hash("$2b$12$" + "a" * 53)
    assert out["candidates"] == ["bcrypt"]


async def test_identify_no_match():
    out = await encoding.identify_hash("definitely not a hash!")
    assert out["candidates"] == []


# --- registration -------------------------------------------------------------------

def test_skills_are_registered_and_safe():
    from friday.skills import registry

    registry.autodiscover()
    names = registry.list_skills()
    for name in ("transform_text", "hash_text", "identify_hash"):
        assert name in names
        assert registry.get(name).auto_confirm is True  # pure computation — no confirmation gate
