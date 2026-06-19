"""Deterministic command router — pattern-match unambiguous voice commands and call
the matching skill directly, skipping the LLM entirely.

Why this exists: common commands like "open firefox" otherwise depend on the cloud
LLM picking a tool. When the free tier is rate-limited and routing falls back to a
local model that can't tool-call, the command silently does nothing. For clear
imperatives we short-circuit here: instant, $0, quota-proof.

Conservative by design — only fires on unambiguous imperatives. ANY doubt returns
None so the caller falls through to the LLM path. False negatives are safe; false
positives (acting on a conversational phrase) are not.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from friday.skills.apps import open_app
from friday.skills.desktop import open_url
from friday.skills.windows import close_window, focus_window, list_windows

# Optional confirm hook: async (skill_name, args) -> bool. Consulted before a GATED
# command (close_window) runs, so the deterministic path still honors the confirmation
# gate when a caller provides one (e.g. `sutra chat`). None = run without asking, which
# preserves the voice path's existing behavior (an explicit spoken imperative).
ConfirmFn = Callable[[str, dict[str, Any]], Awaitable[bool]]

# Leading filler ("hey sutra, can you please open firefox") stripped before matching.
_FILLER_PREFIX = re.compile(
    r"^(?:hey\s+sutra|ok\s+sutra|sutra|yeah|yes|hey|ok|okay|um|uh|"
    r"can\s+you|could\s+you|would\s+you|will\s+you|please)\b[\s,]*",
    re.IGNORECASE,
)
# Trailing filler ("... for me now please?") stripped before matching.
_FILLER_SUFFIX = re.compile(
    r"[\s,]*(?:for\s+me|right\s+now|now|please)*[\s,]*\??$",
    re.IGNORECASE,
)

# A URL or bare domain: scheme, www., or name.<known-tld>.
_URL_RE = re.compile(
    r"(?:^[a-z]+://)|(?:^www\.)|(?:^[a-z0-9][a-z0-9-]*\.(?:com|org|net|io|dev|ai|co|gov|edu)\b)",
    re.IGNORECASE,
)

# Spoken site names that are WEBSITES, not desktop apps — "open youtube" must hit the
# browser, not gtk-launch a non-existent 'youtube' app (the live failure: it claimed
# success and opened nothing). Maps the bare word to the URL to open.
KNOWN_SITES: dict[str, str] = {
    "youtube": "https://youtube.com",
    "yt": "https://youtube.com",
    "gmail": "https://mail.google.com",
    "google": "https://google.com",
    "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com",
    "drive": "https://drive.google.com",
    "calendar": "https://calendar.google.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "reddit": "https://reddit.com",
    "github": "https://github.com",
    "linkedin": "https://linkedin.com",
    "whatsapp": "https://web.whatsapp.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "netflix": "https://netflix.com",
    "spotify": "https://open.spotify.com",
    "amazon": "https://amazon.com",
    "wikipedia": "https://wikipedia.org",
    "tryhackme": "https://tryhackme.com",
    "hackthebox": "https://hackthebox.com",
    "stackoverflow": "https://stackoverflow.com",
    "stack overflow": "https://stackoverflow.com",
}

# Command verbs, anchored at the start of the cleaned text.
_OPEN_RE = re.compile(r"^(?:open|launch|start|fire\s+up)\s+(.+)$", re.IGNORECASE)
_CLOSE_RE = re.compile(r"^close\s+(.+)$", re.IGNORECASE)
_FOCUS_RE = re.compile(
    r"^(?:switch\s+to|focus(?:\s+on)?|bring\s+up|go\s+to)\s+(.+)$", re.IGNORECASE
)
_LIST_RE = re.compile(
    r"^(?:what\s+windows\s+are\s+open|list\s+(?:my\s+|the\s+|open\s+)*windows|"
    r"what(?:'s|\s+is)\s+open)$",
    re.IGNORECASE,
)

# Words that, immediately after "open", mark the phrase as conversational, not a
# launch command ("open up to me", "open to ideas", "open about it", "open minded").
_CONVERSATIONAL_AFTER_OPEN = {"up", "to", "about", "minded"}

# A launch/focus/close target is a short NAME ("firefox", "vs code",
# "tryhackme.com") — never a sentence. These markers, or simply being long, flag a
# COMPOUND or conversational phrase ("open X and start a room and finish it",
# "go to my room then submit") that this deterministic shortcut must NOT run as an
# app — it has to reach the LLM, which can pick web_task_vision etc. Catching this
# is the whole "conservative, any doubt → None" promise of this module.
_SENTENCE_MARKER = re.compile(
    r"\b(?:and|then|so|but|also|after|or|let'?s|let\s+us)\b|\?", re.IGNORECASE
)


def _is_sentence_like(obj: str) -> bool:
    """True when the command object is too long or clause-y to be a real target."""
    return len(obj.split()) > 3 or bool(_SENTENCE_MARKER.search(obj))


# Words that mean the target is a web/CTF thing, never a desktop app — so "open a
# hard room", "open the website", "open my box" must go to the LLM (web_task_vision),
# never gtk-launch. Caught the live "open any hard room" → gtk-launch garbage.
_NOT_AN_APP = re.compile(
    r"\b(room|box|machine|lab|challenge|target|website|site|web ?page|page|tab|url|link)\b",
    re.I,
)


def _strip_filler(text: str) -> str:
    """Lower-noise normalization: collapse whitespace, strip leading/trailing filler.

    Filler can stack ("hey sutra can you please ..."), so strip the leading pattern
    repeatedly until it stops shrinking. Casing is preserved for the object token.
    """
    cleaned = re.sub(r"\s+", " ", text).strip()
    prev = None
    while cleaned != prev:
        prev = cleaned
        cleaned = _FILLER_PREFIX.sub("", cleaned).strip()
    cleaned = _FILLER_SUFFIX.sub("", cleaned).strip()
    return cleaned


def _looks_like_url(token: str) -> bool:
    """True if the object is a URL or bare domain rather than an app name."""
    return bool(_URL_RE.search(token.strip()))


def _normalize_url(token: str) -> str:
    """Prefix https:// if the token has no scheme of its own."""
    token = token.strip()
    if "://" in token:
        return token
    return f"https://{token}"


async def _safe(skill: Any, *args: Any) -> dict[str, Any]:
    """Await a skill, converting any raised exception into a clean error dict."""
    try:
        result = await skill(*args)
    except Exception as exc:  # noqa: BLE001 — never raise into the agent loop
        return {"error": f"{skill.__name__} failed: {exc}"}
    return result if isinstance(result, dict) else {"result": result}


def _result(spoken: str, skill: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"spoken": spoken, "skill": skill, "result": result}


async def try_command(text: str, confirm: ConfirmFn | None = None) -> dict[str, Any] | None:
    """Match `text` against known direct commands and run the matching skill.

    Returns {"spoken": str, "skill": str, "result": dict} on a confident match,
    else None so the caller falls through to the LLM tool-calling path.

    confirm: optional async (skill_name, args) -> bool, consulted before a GATED
    command (close_window) runs. None → run without asking (voice's behavior).
    """
    if not text or not text.strip():
        return None
    cleaned = _strip_filler(text)
    if not cleaned:
        return None

    # 4. list windows (check before open/etc.; "what's open" has no verb anyway).
    if _LIST_RE.match(cleaned):
        result = await _safe(list_windows)
        windows = result.get("windows", []) if isinstance(result, dict) else []
        n = len(windows)
        spoken = f"You have {n} window{'s' if n != 1 else ''} open."
        return _result(spoken, "list_windows", result)

    # 1. open / launch / start / fire up <X>
    if m := _OPEN_RE.match(cleaned):
        obj = m.group(1).strip()
        first_word = obj.split(maxsplit=1)[0].lower() if obj else ""
        if not obj or first_word in _CONVERSATIONAL_AFTER_OPEN:
            return None
        if _is_sentence_like(obj):
            return None  # compound/conversational ("open X and finish it") → LLM
        if _NOT_AN_APP.search(obj):
            return None  # "open a hard room" / "open the website" → web agent, not gtk-launch
        if _looks_like_url(obj):
            url = _normalize_url(obj)
            result = await _safe(open_url, url)
            return _result(f"Opening {obj}.", "open_url", result)
        if site := KNOWN_SITES.get(obj.lower()):
            result = await _safe(open_url, site)
            return _result(f"Opening {obj}.", "open_url", result)
        result = await _safe(open_app, obj)
        if isinstance(result, dict) and result.get("error"):
            return _result(f"I couldn't find {obj} on your system.", "open_app", result)
        return _result(f"Opening {obj}.", "open_app", result)

    # 3. switch to / focus / bring up / go to <X>
    if m := _FOCUS_RE.match(cleaned):
        obj = m.group(1).strip()
        if not obj or _is_sentence_like(obj):
            return None
        if _looks_like_url(obj):
            url = _normalize_url(obj)
            result = await _safe(open_url, url)
            return _result(f"Opening {obj}.", "open_url", result)
        result = await _safe(focus_window, obj)
        return _result(f"Switching to {obj}.", "focus_window", result)

    # 2. close <X>  (strip a trailing "window"; skip bare generic "the tab"/"window")
    if m := _CLOSE_RE.match(cleaned):
        obj = m.group(1).strip()
        obj = re.sub(r"\s+window$", "", obj, flags=re.IGNORECASE).strip()
        # Reject generic/empty targets: bare "window"/"tab", an article alone, or "it".
        if not obj or obj.lower() in {"the", "a", "tab", "the tab", "window", "it", "this"}:
            return None
        if _is_sentence_like(obj):  # "close X and open Y" → LLM, not a blind close
            return None
        # close_window is GATED (irreversible — unsaved work). Honor a caller's confirm
        # hook before closing; without one, run (parity with the voice path).
        if confirm is not None and not await confirm("close_window", {"title": obj}):
            return _result(f"Okay, I'll leave {obj} open.", "close_window", {"status": "declined"})
        result = await _safe(close_window, obj)
        return _result(f"Closing {obj}.", "close_window", result)

    return None
