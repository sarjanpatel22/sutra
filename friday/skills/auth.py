"""Auth-token analysis — JWT weaknesses, the payable auth-flaw class.

analyze_jwt decodes a JWT and flags the issues that turn into account takeover / privilege
escalation: alg:none (signature unchecked → forge any token), a weak HMAC secret (cracked
against a built-in list or your wordlist → forge any token), a missing/expired expiry, and the
privilege/identity claims worth tampering once a token is forgeable. PASSIVE, pure-stdlib (base64,
hmac) — it analyses a token you hold, sends no requests; you forge/replay and verify yourself.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import Any

from friday.skills.registry import skill

# Embarrassingly-common HMAC signing secrets — the default, instant "is it weak?" pass. Pass a
# wordlist= to go deeper. (Includes real defaults shipped by tutorials/boilerplates.)
_WEAK_SECRETS = (
    "secret",
    "password",
    "123456",
    "changeme",
    "admin",
    "root",
    "key",
    "jwt",
    "token",
    "secretkey",
    "secret_key",
    "your-256-bit-secret",
    "your_jwt_secret",
    "jwt_secret",
    "jwtsecret",
    "mysecret",
    "supersecret",
    "s3cr3t",
    "qwerty",
    "letmein",
    "test",
    "dev",
    "prod",
    "default",
    "secret123",
    "password123",
    "P@ssw0rd",
    "topsecret",
    "privatekey",
    "example_key",
    "ChangeMe",
)

# Claims worth tampering once forgeable: privilege (role/scope/…) and identity (sub/id/…).
_SENSITIVE_CLAIMS = (
    "role",
    "roles",
    "admin",
    "is_admin",
    "isadmin",
    "scope",
    "scopes",
    "permissions",
    "perms",
    "groups",
    "tier",
    "plan",
    "level",
    "user_id",
    "userid",
    "account_id",
    "accountid",
    "sub",
    "uid",
    "id",
    "user",
    "username",
    "is_superuser",
    "superuser",
)


def _b64url_decode(seg: str) -> bytes:
    """Decode a base64url segment, restoring the padding JWTs strip."""
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def _crack_hs256(signing_input: bytes, signature: bytes, candidates: list[str]) -> str | None:
    """Return the secret whose HMAC-SHA256 over the signing input matches the token's signature."""
    for sec in candidates:
        mac = hmac.new(sec.encode(), signing_input, hashlib.sha256).digest()
        if hmac.compare_digest(mac, signature):
            return sec
    return None


@skill(
    name="analyze_jwt",
    description=(
        "Decode and audit a JWT for the payable auth flaws: the alg:none signature "
        "bypass, a weak HMAC secret (cracked against a built-in list or a wordlist you pass "
        "— a hit means you can FORGE any token), a missing or already-expired expiry, and the "
        "privilege/identity claims (role, admin, scope, sub, id) worth tampering once a token is "
        "forgeable. Returns the decoded header + payload and findings. Passive analysis of "
        "a token you already hold — no requests are sent; you forge/replay and verify."
    ),
    auto_confirm=True,  # decodes + local analysis of a provided token, no target interaction
    timeout_s=30,
)
async def analyze_jwt(token: str, wordlist: str = "") -> dict[str, Any]:
    """Decode a JWT and flag its weaknesses.

    token: the JWT string (header.payload.signature).
    wordlist: optional path to a secrets wordlist to extend the built-in weak-secret crack.
    """
    token = (token or "").strip()
    if not token:
        return {"error": "no token to analyze"}
    parts = token.split(".")
    if len(parts) != 3:
        return {"error": "not a JWT (expected 3 dot-separated parts: header.payload.signature)"}
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, json.JSONDecodeError) as e:
        return {"error": f"could not decode the JWT: {e}"}
    if not isinstance(payload, dict):
        return {"error": "JWT payload is not a JSON object"}

    alg = str(header.get("alg", "")).strip()
    findings: list[dict[str, Any]] = []
    forgeable = False
    cracked_secret: str | None = None

    # alg:none — the signature isn't verified, so any token can be forged.
    if alg.lower() == "none":
        forgeable = True
        findings.append(
            {
                "id": "jwt-alg-none",
                "severity": "critical",
                "name": "JWT accepts alg:none — signature not verified",
                "description": (
                    "The token header sets alg=none. If the server honours it, the signature "
                    "is never checked and any token (with any claims) is accepted. Forge a "
                    "token with elevated claims and replay to confirm."
                ),
            }
        )

    # Weak HMAC secret — crack it → you can sign arbitrary tokens.
    if alg.upper().startswith("HS"):
        candidates = list(_WEAK_SECRETS)
        wl = (wordlist or "").strip()
        if wl:
            try:
                with open(wl, encoding="utf-8", errors="ignore") as fh:
                    candidates += [ln.strip() for ln in fh if ln.strip()][:200_000]
            except OSError as e:
                findings.append(
                    {
                        "id": "jwt-wordlist-error",
                        "severity": "info",
                        "name": "wordlist could not be read",
                        "description": f"{e}; used the built-in weak-secret list only.",
                    }
                )
        signing_input = f"{parts[0]}.{parts[1]}".encode()
        try:
            signature = _b64url_decode(parts[2])
        except ValueError:
            signature = b""
        cracked_secret = (
            await asyncio.to_thread(_crack_hs256, signing_input, signature, candidates)
            if signature
            else None
        )
        if cracked_secret is not None:
            forgeable = True
            findings.append(
                {
                    "id": "jwt-weak-secret",
                    "severity": "critical",
                    "name": f"JWT HMAC secret is weak — cracked to '{cracked_secret}'",
                    "description": (
                        f"The {alg} signing secret is '{cracked_secret}'. With the secret you "
                        "can sign arbitrary tokens (any user, any role). Forge one and replay."
                    ),
                }
            )

    # Expiry hygiene.
    now = int(time.time())
    if "exp" not in payload:
        findings.append(
            {
                "id": "jwt-no-expiry",
                "severity": "medium",
                "name": "JWT has no exp (expiry) claim",
                "description": "The token never expires; a leaked/captured token is valid "
                "forever. Combined with a forgeable signature this is worse.",
            }
        )
    elif isinstance(payload.get("exp"), (int, float)) and payload["exp"] < now:
        findings.append(
            {
                "id": "jwt-expired",
                "severity": "info",
                "name": "JWT is already expired",
                "description": f"exp={payload['exp']} is in the past (now={now}).",
            }
        )

    # Privilege/identity claims worth tampering once the token is forgeable.
    present = [c for c in _SENSITIVE_CLAIMS if c in payload]
    if present:
        sev = "high" if forgeable else "low"
        how = (
            "You can forge this token, so tamper these to escalate (set role=admin, change the "
            "user id, …) and replay."
            if forgeable
            else "Tamper candidates IF you can forge the signature (alg confusion, weak secret, "
            "or a leaked key)."
        )
        findings.append(
            {
                "id": "jwt-tamperable-claims",
                "severity": sev,
                "name": f"Privilege/identity claims present: {', '.join(present)}",
                "description": f"Claims {present} control access or identity. {how}",
            }
        )

    # RS256/ES256 → algorithm-confusion candidate (try re-signing as HS256 with the public key).
    if alg.upper().startswith(("RS", "ES", "PS")):
        findings.append(
            {
                "id": "jwt-alg-confusion-candidate",
                "severity": "info",
                "name": f"Asymmetric alg ({alg}) — try the HS/RS algorithm-confusion attack",
                "description": (
                    "If the server uses one verify call for all algs, re-sign the token as "
                    "HS256 using the server's PUBLIC key as the HMAC secret. Fetch the public "
                    "key (JWKS/cert) and test."
                ),
            }
        )

    findings.sort(
        key=lambda f: {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}.get(
            f["severity"], 5
        )
    )
    return {
        "alg": alg,
        "header": header,
        "payload": payload,
        "forgeable": forgeable,
        "cracked_secret": cracked_secret,
        "findings": findings,
        "vulnerable": any(f["severity"] in ("critical", "high", "medium") for f in findings),
        "note": (
            "Findings are candidates — forge/tamper a token and REPLAY it against the target to "
            "confirm the server actually accepts it before reporting."
        ),
    }
