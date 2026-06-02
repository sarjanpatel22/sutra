#!/usr/bin/env python
"""Memory smoke test — run after `docker compose up -d`.

Usage:
  python scripts/memory_smoke.py write   # store a few facts (run in session 1)
  python scripts/memory_smoke.py read    # recall them      (run in session 2)
  python scripts/memory_smoke.py         # write then read in one go

Validates the Phase 1.3 exit criterion: mention facts, recall them later.
"""

import asyncio
import sys

from friday.memory.store import get_store

FACTS = [
    "Sarjan is a software engineer at Acme Corp, based in Toronto.",
    "Sarjan is building Sutra, a voice-native ambient AI assistant.",
    "Sutra runs free-by-default: Ollama gemma3:4b locally, Groq/Gemini as free fallback.",
]
QUERIES = ["Where does Sarjan work?", "What is Sarjan building?", "How does Sutra stay free?"]


async def _write(store) -> None:
    print("Writing facts (entity extraction runs via Ollama — may take a few seconds each):")
    for f in FACTS:
        ok = await store.remember(f, source="smoke-test")
        print(f"  [{'ok' if ok else 'FAIL'}] {f}")


async def _read(store) -> None:
    print("\nRecalling:")
    for q in QUERIES:
        facts = await store.recall(q, limit=3)
        print(f"  Q: {q}")
        for fact in facts:
            print(f"     - {fact}")
        if not facts:
            print("     (no facts found)")


async def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    store = get_store()
    if not await store.initialize():
        print("Memory unavailable — is FalkorDB up? Try: docker compose up -d")
        sys.exit(1)
    if mode in ("write", "both"):
        await _write(store)
    if mode in ("read", "both"):
        await _read(store)
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
