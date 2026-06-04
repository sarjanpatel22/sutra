"""Find — and only with --apply, delete — junk episodes in the memory graph.

Far-field / echoing voice turns get transcribed as garbage ("M-H-H-H-H", repeated
characters) and persisted, polluting recall. This scans every episode, flags the
ones whose user transcript reads like noise (the same `looks_like_noise` gate that
now blocks new ones), and shows them.

PREVIEW BY DEFAULT — deletes nothing. Look at the list, then re-run with --apply.
Mirrors `forget`'s preview-first safety: deletion is destructive and irreversible.

    python scripts/clean_memory.py            # preview the junk
    python scripts/clean_memory.py --apply    # delete it
"""
from __future__ import annotations

import argparse
import asyncio
import re

from friday.memory.store import get_store, looks_like_noise

# Conversation episodes are "User: <transcript>\nSutra: <reply>". Judge the user's
# transcript; for non-conversation episodes (seeded facts, ambient) judge the whole.
_USER_RE = re.compile(r"\s*User:\s*(.*?)\s*\nSutra:", re.DOTALL)


def _candidate_text(content: str) -> str:
    m = _USER_RE.match(content)
    return m.group(1) if m else content


def _junk_episodes(episodes: list[dict[str, str]]) -> list[dict[str, str]]:
    return [e for e in episodes if looks_like_noise(_candidate_text(e["content"]))]


async def main() -> int:
    ap = argparse.ArgumentParser(description="Remove junk episodes from memory.")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: preview)")
    args = ap.parse_args()

    store = get_store()
    episodes = store.list_episodes()
    if not episodes:
        print("No episodes found (FalkorDB down, or empty graph).")
        return 1

    junk = _junk_episodes(episodes)
    print(f"Scanned {len(episodes)} episodes — {len(junk)} look like noise:\n")
    for e in junk:
        snippet = " ".join(e["content"].split())[:80]
        print(f"  [{e['uuid'][:8]}] {e['name'][:28]:28}  {snippet!r}")

    if not junk:
        print("\nNothing to clean. ✓")
        return 0
    if not args.apply:
        print(f"\nPreview only — {len(junk)} episode(s) would be removed. Re-run with --apply.")
        return 0

    print(f"\nDeleting {len(junk)} episode(s)…")
    removed = 0
    for e in junk:
        if await store.remove_episode(e["uuid"]):
            removed += 1
    print(f"Removed {removed}/{len(junk)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
