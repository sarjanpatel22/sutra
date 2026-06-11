"""Find — and only with --apply, delete — junk or duplicate episodes.

Three scans, all PREVIEW BY DEFAULT (delete nothing; look, then re-run --apply):

  noise  (default)  Far-field / echoing voice turns transcribe as garbage
                    ("M-H-H-H-H", repeated chars) and get persisted. Flags
                    episodes whose user transcript reads like noise.

  --lowvalue        Ambient activity telemetry ("spent 8m in Terminal…") and
                    dialogue narration ("The User is talking to Sutra") — coherent
                    but factless episodes that flood the graph and out-rank real
                    facts (measured: ~60% of episodes, tanking recall hit@1).
                    Never flags a verbatim "User:/Sutra:" turn (can hold a fact).

  --dupes           Near-duplicate episodes: re-seeding writes the same fact as a
                    fresh node, and ambient capture can echo. Clusters episodes by
                    token-set (Jaccard) similarity and keeps one per cluster — no
                    embedder needed, so it runs offline against FalkorDB alone.

    python scripts/clean_memory.py                    # preview noise
    python scripts/clean_memory.py --apply             # delete noise
    python scripts/clean_memory.py --lowvalue          # preview ambient/narration
    python scripts/clean_memory.py --lowvalue --apply  # delete low-value episodes
    python scripts/clean_memory.py --dupes             # preview near-duplicates
    python scripts/clean_memory.py --dupes --apply     # delete extra copies

Mirrors `forget`'s preview-first safety: deletion is destructive and irreversible.
"""
from __future__ import annotations

import argparse
import asyncio
import re

from friday.memory.store import get_store, looks_like_noise, looks_low_value

# Conversation episodes are "User: <transcript>\nSutra: <reply>". Judge the user's
# transcript; for non-conversation episodes (seeded facts, ambient) judge the whole.
_USER_RE = re.compile(r"\s*User:\s*(.*?)\s*\nSutra:", re.DOTALL)
_WORD_RE = re.compile(r"[a-z0-9]+")


def _candidate_text(content: str) -> str:
    m = _USER_RE.match(content)
    return m.group(1) if m else content


def _junk_episodes(episodes: list[dict[str, str]]) -> list[dict[str, str]]:
    return [e for e in episodes if looks_like_noise(_candidate_text(e["content"]))]


# ── near-duplicate detection ─────────────────────────────────────────────────
def _tokens(content: str) -> frozenset[str]:
    """Normalized token set: lowercase alphanumeric words, order/punctuation-free."""
    return frozenset(_WORD_RE.findall(content.lower()))


def _similar(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity of two token sets (1.0 = identical word bag)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _dup_clusters(
    episodes: list[dict[str, str]], threshold: float
) -> list[list[dict[str, str]]]:
    """Greedily group episodes whose token sets are ≥ threshold similar.

    Returns only clusters with >1 member (the actual duplicates). Each episode's
    token set is cached once; comparison is O(n²) on the set, fine for thousands.
    """
    toks = [_tokens(e["content"]) for e in episodes]
    assigned = [False] * len(episodes)
    clusters: list[list[dict[str, str]]] = []
    for i in range(len(episodes)):
        if assigned[i] or not toks[i]:
            continue
        cluster = [episodes[i]]
        assigned[i] = True
        for j in range(i + 1, len(episodes)):
            if assigned[j]:
                continue
            if _similar(toks[i], toks[j]) >= threshold:
                cluster.append(episodes[j])
                assigned[j] = True
        if len(cluster) > 1:
            clusters.append(cluster)
    return clusters


async def _clean_noise(store, episodes: list[dict[str, str]], apply: bool) -> int:
    junk = _junk_episodes(episodes)
    print(f"Scanned {len(episodes)} episodes — {len(junk)} look like noise:\n")
    for e in junk:
        snippet = " ".join(e["content"].split())[:80]
        print(f"  [{e['uuid'][:8]}] {e['name'][:28]:28}  {snippet!r}")
    if not junk:
        print("\nNothing to clean. ✓")
        return 0
    if not apply:
        print(f"\nPreview only — {len(junk)} episode(s) would be removed. Re-run with --apply.")
        return 0
    print(f"\nDeleting {len(junk)} episode(s)…")
    removed = sum([await store.remove_episode(e["uuid"]) for e in junk])
    print(f"Removed {removed}/{len(junk)}.")
    return 0


async def _clean_lowvalue(store, episodes: list[dict[str, str]], apply: bool) -> int:
    junk = [e for e in episodes if looks_low_value(e["content"])]
    print(
        f"Scanned {len(episodes)} episodes — {len(junk)} are low-value "
        f"(activity telemetry / dialogue narration, no recall value):\n"
    )
    for e in junk:
        snippet = " ".join(e["content"].split())[:80]
        print(f"  [{e['uuid'][:8]}] {snippet!r}")
    if not junk:
        print("\nNothing to clean. ✓")
        return 0
    if not apply:
        print(f"\nPreview only — {len(junk)} episode(s) would be removed. Re-run with --apply.")
        return 0
    print(f"\nDeleting {len(junk)} episode(s)…")
    removed = sum([await store.remove_episode(e["uuid"]) for e in junk])
    print(f"Removed {removed}/{len(junk)}.")
    return 0


async def _clean_dupes(
    store, episodes: list[dict[str, str]], threshold: float, apply: bool
) -> int:
    clusters = _dup_clusters(episodes, threshold)
    extra = sum(len(c) - 1 for c in clusters)  # all but one kept per cluster
    print(
        f"Scanned {len(episodes)} episodes — {len(clusters)} duplicate cluster(s), "
        f"{extra} redundant copy/copies (similarity ≥ {threshold:.0%}):\n"
    )
    to_remove: list[dict[str, str]] = []
    for n, cluster in enumerate(clusters, 1):
        keep, drop = cluster[0], cluster[1:]
        to_remove += drop
        snippet = " ".join(keep["content"].split())[:70]
        print(f"  cluster {n} ({len(cluster)} copies)  keep [{keep['uuid'][:8]}] {snippet!r}")
        for e in drop:
            print(f"            drop [{e['uuid'][:8]}] {e['name'][:24]}")
    if not to_remove:
        print("\nNo duplicates. ✓")
        return 0
    if not apply:
        print(f"\nPreview only — {len(to_remove)} copy/copies would be removed; re-run --apply.")
        return 0
    print(f"\nDeleting {len(to_remove)} redundant copy/copies…")
    removed = sum([await store.remove_episode(e["uuid"]) for e in to_remove])
    print(f"Removed {removed}/{len(to_remove)} (one kept per cluster).")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description="Remove junk or duplicate episodes from memory.")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: preview)")
    ap.add_argument("--dupes", action="store_true", help="scan near-duplicates instead of noise")
    ap.add_argument(
        "--lowvalue",
        action="store_true",
        help="scan low-value ambient telemetry / dialogue narration instead of noise",
    )
    ap.add_argument("--threshold", type=float, default=0.9, help="dup similarity cutoff (0–1)")
    args = ap.parse_args()

    store = get_store()
    episodes = store.list_episodes()
    if not episodes:
        print("No episodes found (FalkorDB down, or empty graph).")
        return 1

    if args.dupes:
        return await _clean_dupes(store, episodes, args.threshold, args.apply)
    if args.lowvalue:
        return await _clean_lowvalue(store, episodes, args.apply)
    return await _clean_noise(store, episodes, args.apply)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
