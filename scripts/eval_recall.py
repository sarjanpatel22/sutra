"""Measure recall quality on the seeded facts — hit@1 / hit@3 / hit@5.

Recall is "decent-not-perfect"; this turns that into a number so any future
embedder/reranker change can be judged against a baseline instead of vibes
(the same "measure first" lesson that killed the needless qwen3-embedding swap).

A fixed (query → expected-substring) probe set targets facts the seeder writes
(`scripts/seed_memory.py`). For each query we recall the top-5 facts and find the
rank of the first fact containing the expected substring; hit@k means that rank
is ≤ k. Prints a per-query table and the aggregate hit@k.

    python scripts/eval_recall.py            # needs FalkorDB up + graph seeded
    python scripts/eval_recall.py --limit 8  # widen the recall window

Seed first if the graph is empty:  python scripts/seed_memory.py
"""

from __future__ import annotations

import argparse
import asyncio

from friday.memory.store import get_store

# (query, expected-substring) — substring matched case-insensitively against the
# returned fact strings. Each targets a distinct seeded fact; keep these phrased
# the way a user would actually ask, not echoing the stored sentence.
PROBES: list[tuple[str, str]] = [
    ("Where is Sarjan from?", "sudbury"),
    ("Who does Sarjan work for?", "scale"),
    ("What is Sarjan building?", "sutra"),
    ("What database does the memory layer use?", "falkordb"),
    ("Which model extracts memories?", "qwen"),
    ("What does the voice loop use for text to speech?", "kokoro"),
    ("What does the voice loop use for speech to text?", "whisper"),
    ("What operating system does the machine run?", "linux"),
]


def _rank_of(facts: list[str], needle: str) -> int | None:
    """1-based rank of the first fact containing needle, or None if absent."""
    needle = needle.lower()
    for i, fact in enumerate(facts, 1):
        if needle in fact.lower():
            return i
    return None


async def main() -> int:
    ap = argparse.ArgumentParser(description="Measure recall hit@k on seeded facts.")
    ap.add_argument("--limit", type=int, default=5, help="recall window (default: 5)")
    args = ap.parse_args()
    k_set = (1, 3, 5)

    store = get_store()
    if not await store.initialize():
        print("✗ memory store unavailable — is FalkorDB running?")
        return 1

    hits = {k: 0 for k in k_set}
    misses: list[str] = []
    print(f"Probing recall on {len(PROBES)} queries (window={args.limit})…\n")
    print(f"  {'rank':>4}  query")
    print(f"  {'─' * 4}  {'─' * 50}")
    for query, needle in PROBES:
        facts = await store.recall(query, limit=args.limit)
        rank = _rank_of(facts, needle)
        for k in k_set:
            if rank is not None and rank <= k:
                hits[k] += 1
        mark = str(rank) if rank is not None else "—"
        if rank is None:
            misses.append(f"{query}  (wanted '{needle}')")
        print(f"  {mark:>4}  {query}")
    await store.close()

    n = len(PROBES)
    print(f"\n── hit@k over {n} probes ──")
    for k in k_set:
        pct = 100.0 * hits[k] / n
        print(f"  hit@{k}: {hits[k]}/{n}  ({pct:.0f}%)")
    if misses:
        print(f"\n  misses (not in top-{args.limit}):")
        for m in misses:
            print(f"    ✗ {m}")
    else:
        print("\n  ✓ every probe recalled within the window.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
