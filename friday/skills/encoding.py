"""Encode / decode / hash skills — the CTF & recon bread-and-butter primitives.

All pure-stdlib (base64, binascii, urllib.parse, codecs, hashlib): zero new dependencies, so they
can never break a clean install, and they run instantly and offline. These are the operations
reached for constantly on TryHackMe — flip a base64/hex/url/rot13 blob, hash a string, or identify
an unknown digest. One skill per operation FAMILY (with an `operation`/`algorithm` selector) rather
than a dozen tiny skills, to keep the tool schema — and the model's tool choice — lean.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
import urllib.parse
from typing import Any

from friday.skills.registry import skill

_TRANSFORMS = (
    "base64_encode", "base64_decode",
    "hex_encode", "hex_decode",
    "url_encode", "url_decode",
    "rot13",
)


@skill(
    name="transform_text",
    description=(
        "Encode or decode text with a common scheme — base64, hex, URL-encoding, or ROT13. THE "
        "tool for CTF/recon string flips (decode a base64 flag, hex-decode a blob, rot13 a hint). "
        "operation is one of: " + ", ".join(_TRANSFORMS) + "."
    ),
    auto_confirm=True,
    timeout_s=5,
)
async def transform_text(text: str, operation: str) -> dict[str, Any]:
    """Encode or decode `text` using `operation`.

    text: The input string to transform.
    operation: One of base64_encode, base64_decode, hex_encode, hex_decode, url_encode,
        url_decode, rot13.
    """
    op = (operation or "").strip().lower().replace("-", "_")
    try:
        if op == "base64_encode":
            out = base64.b64encode(text.encode()).decode()
        elif op == "base64_decode":
            out = base64.b64decode(text.encode(), validate=False).decode("utf-8", "replace")
        elif op == "hex_encode":
            out = text.encode().hex()
        elif op == "hex_decode":
            out = bytes.fromhex(text.strip().replace(" ", "")).decode("utf-8", "replace")
        elif op == "url_encode":
            out = urllib.parse.quote(text, safe="")
        elif op == "url_decode":
            out = urllib.parse.unquote(text)
        elif op == "rot13":
            out = codecs.encode(text, "rot_13")
        else:
            return {
                "error": f"unknown operation {operation!r}; use one of {', '.join(_TRANSFORMS)}"
            }
    except (binascii.Error, ValueError, UnicodeDecodeError) as e:
        # malformed input is returned to the model as an error, never raised
        return {"error": f"could not {op}: {e}", "input": text}
    return {"operation": op, "input": text, "result": out}


_HASHES = ("md5", "sha1", "sha256", "sha512")


@skill(
    name="hash_text",
    description=(
        "Compute a cryptographic hash digest (hex) of text — md5, sha1, sha256, or sha512. Use to "
        "hash a password/string or produce an integrity digest. algorithm defaults to all four. "
        "For encoding/decoding use transform_text instead."
    ),
    auto_confirm=True,
    timeout_s=5,
)
async def hash_text(text: str, algorithm: str = "all") -> dict[str, Any]:
    """Hash `text` and return the hex digest(s).

    text: The string to hash.
    algorithm: md5, sha1, sha256, sha512, or all (default) for every one.
    """
    algo = (algorithm or "all").strip().lower()
    wanted = _HASHES if algo in ("", "all") else (algo,)
    digests: dict[str, str] = {}
    for name in wanted:
        if name not in _HASHES:
            return {
                "error": f"unknown algorithm {algorithm!r}; use one of {', '.join(_HASHES)} or all"
            }
        digests[name] = hashlib.new(name, text.encode()).hexdigest()
    return {"input": text, "digests": digests}


# (length, charset) → likely hash types. Heuristic — many hashes share a length, so this returns
# candidates, not a verdict. crypt-style prefixes ($2/$1/$5/$6) are unambiguous and handled first.
_HEX_BY_LEN = {
    32: ["MD5", "NTLM", "MD4"],
    40: ["SHA-1"],
    56: ["SHA-224"],
    64: ["SHA-256", "SHA3-256"],
    96: ["SHA-384"],
    128: ["SHA-512", "SHA3-512"],
}


@skill(
    name="identify_hash",
    description=(
        "Guess what kind of hash a string is from its length and character set (32 hex = MD5/NTLM, "
        "40 = SHA-1, 64 = SHA-256, $2... = bcrypt). For unknown digests found during CTF/recon. "
        "Heuristic — returns likely candidates, best guess first."
    ),
    auto_confirm=True,
    timeout_s=5,
)
async def identify_hash(value: str) -> dict[str, Any]:
    """Identify the likely hash type(s) of `value` by length and character set.

    value: The hash string to identify.
    """
    v = (value or "").strip()
    if not v:
        return {"value": v, "candidates": [], "note": "empty input"}
    candidates: list[str] = []
    if v.startswith("$2") and v[3:].startswith("$"):
        candidates = ["bcrypt"]
    elif v.startswith("$1$"):
        candidates = ["md5crypt"]
    elif v.startswith("$5$"):
        candidates = ["sha256crypt"]
    elif v.startswith("$6$"):
        candidates = ["sha512crypt"]
    elif all(c in "0123456789abcdefABCDEF" for c in v) and len(v) in _HEX_BY_LEN:
        candidates = list(_HEX_BY_LEN[len(v)])
    if not candidates:
        return {"value": v, "length": len(v), "candidates": [],
                "note": "no confident match by length/charset"}
    return {"value": v, "length": len(v), "candidates": candidates}
