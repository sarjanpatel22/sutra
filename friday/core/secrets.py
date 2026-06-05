"""Secret broker — OS keyring first, environment (.env) fallback.

The spec mandates keyring for secrets, not plaintext .env. This reads secrets from
the OS keyring (service "sutra") and falls back to the environment so existing
setups keep working. ``load_into_env()`` mirrors keyring secrets into os.environ at
startup so libraries that read the env directly (litellm) pick them up. Every
keyring call is guarded — a missing or locked backend degrades to env, never crashes.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("sutra.secrets")

_SERVICE = "sutra"

# The secrets Sutra knows about — used by `secrets import` / load_into_env / status.
KNOWN_SECRETS = (
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


def _keyring() -> object | None:
    try:
        import keyring
        return keyring
    except Exception:  # noqa: BLE001 — keyring not installed / import error
        return None


def get_secret(name: str, default: str | None = None) -> str | None:
    """Resolve a secret: keyring first, then environment, then default."""
    kr = _keyring()
    if kr is not None:
        try:
            val = kr.get_password(_SERVICE, name)  # type: ignore[attr-defined]
            if val:
                return val
        except Exception as e:  # noqa: BLE001 — locked/missing backend → fall back to env
            logger.debug("keyring read failed for %s: %s", name, e)
    return os.getenv(name, default)


def set_secret(name: str, value: str) -> bool:
    """Store a secret in the keyring. Returns False if no backend is available."""
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(_SERVICE, name, value)  # type: ignore[attr-defined]
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("keyring write failed for %s: %s", name, e)
        return False


def import_from_env(names: tuple[str, ...] = KNOWN_SECRETS) -> list[str]:
    """Copy any env-present secrets into the keyring. Returns the names migrated."""
    return [name for name in names if (v := os.getenv(name)) and set_secret(name, v)]


def load_into_env(names: tuple[str, ...] = KNOWN_SECRETS) -> int:
    """Populate os.environ from keyring for libraries that read env directly.

    Does not override an env var that's already set (so an explicit .env wins).
    Returns how many secrets were loaded from keyring.
    """
    kr = _keyring()
    if kr is None:
        return 0
    n = 0
    for name in names:
        if os.getenv(name):
            continue
        try:
            v = kr.get_password(_SERVICE, name)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            v = None
        if v:
            os.environ[name] = v
            n += 1
    return n


def status(names: tuple[str, ...] = KNOWN_SECRETS) -> dict[str, str]:
    """Where each secret resolves from: 'keyring', 'env', or 'missing'."""
    kr = _keyring()
    out: dict[str, str] = {}
    for name in names:
        in_keyring = False
        if kr is not None:
            try:
                in_keyring = bool(kr.get_password(_SERVICE, name))  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                in_keyring = False
        out[name] = "keyring" if in_keyring else ("env" if os.getenv(name) else "missing")
    return out
