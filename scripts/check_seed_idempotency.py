"""Step-7 safety check: seeding the memory graph twice must be idempotent.

Each `remember()` writes a fresh, timestamped Episodic node (so episode count
*is* expected to grow), but Graphiti resolves Entity nodes by name within the
group — so re-running the seeder must NOT create a second "Sudbury" / "Sarjan" /
"Toronto" entity. This script proves that against the live FalkorDB:

  1. snapshot entity names (+ duplicate check)
  2. run the seeder once more
  3. re-snapshot, and assert no entity name is duplicated
  4. confirm Sudbury (home) and Toronto (work) are present and distinct

Run:  python scripts/check_seed_idempotency.py     (needs FalkorDB up)
This is a live integration check, not a unit test — it talks to real services.
"""

from __future__ import annotations

import asyncio
import os
from collections import Counter

from falkordb import FalkorDB

from scripts.seed_memory import gather_facts

_HOST = os.getenv("FALKORDB_HOST", "localhost")
_PORT = int(os.getenv("FALKORDB_PORT", "6379"))
_GRAPH = os.getenv("MEMORY_GROUP_ID", "sarjan")


def _graph():
    return FalkorDB(host=_HOST, port=_PORT).select_graph(_GRAPH)


def entity_names() -> list[str]:
    g = _graph()
    res = g.query("MATCH (n:Entity) RETURN n.name AS name")
    return [row[0] for row in res.result_set if row[0]]


def count_label(label: str) -> int:
    g = _graph()
    res = g.query(f"MATCH (n:{label}) RETURN count(n) AS c")
    return int(res.result_set[0][0])


def find_entity(substr: str) -> list[str]:
    g = _graph()
    res = g.query(
        "MATCH (n:Entity) WHERE toLower(n.name) CONTAINS toLower($s) "
        "RETURN n.name AS name",
        {"s": substr},
    )
    return [row[0] for row in res.result_set]


async def reseed() -> int:
    from friday.memory.store import get_store

    store = get_store()
    if not await store.initialize():
        raise SystemExit("✗ memory store unavailable — is FalkorDB running?")
    facts = gather_facts()
    for fact in facts:
        await store.remember(fact, source="seed")
    await store.close()
    return len(facts)


def report_duplicates(tag: str) -> int:
    names = entity_names()
    dupes = {n: c for n, c in Counter(names).items() if c > 1}
    print(f"  [{tag}] {len(names)} entity nodes, {len(set(names))} unique")
    if dupes:
        print(f"  [{tag}] ✗ DUPLICATE entities: {dupes}")
    return len(dupes)


async def main() -> None:
    print("── Seeder idempotency check (live FalkorDB) ──\n")

    ep_before = count_label("Episodic")
    print("Before reseed:")
    dupes_before = report_duplicates("before")

    print("\nRe-running the seeder (qwen2.5:14b extraction — this is slow)…")
    n = await reseed()
    print(f"  wrote {n} episodes")

    ep_after = count_label("Episodic")
    print("\nAfter reseed:")
    dupes_after = report_duplicates("after")

    sudbury = find_entity("sudbury")
    toronto = find_entity("toronto")

    print("\n── Results ──")
    print(f"  Episodic nodes: {ep_before} → {ep_after}  "
          "(growth expected; each remember = 1 episode)")
    print(f"  Sudbury entity: {sudbury or '✗ MISSING'}")
    print(f"  Toronto entity: {toronto or '✗ MISSING'}")

    ok = True
    if dupes_after:
        print("  ✗ FAIL: duplicate entity nodes after reseed — not idempotent")
        ok = False
    if not sudbury:
        print("  ✗ FAIL: Sudbury (home) entity missing")
        ok = False
    if not toronto:
        print("  ✗ FAIL: Toronto (work) entity missing")
        ok = False

    if ok:
        print("\n  ✓ PASS: reseed added episodes only, no duplicate entities; "
              "Sudbury + Toronto present and distinct.")
    else:
        raise SystemExit(1)
    # silence unused-var lint for the pre-counts we print
    _ = (dupes_before,)


if __name__ == "__main__":
    asyncio.run(main())
