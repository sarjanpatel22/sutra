"""Write the important facts from a work session into Sutra's memory graph.

The graph is meant to be the single queryable source of truth — so instead of
re-reading files / scanning git / rebuilding context (which burns tokens + time),
Sutra (or its assistant) just recalls from here. Ambient capture handles the raw
OS stream; this script records the *semantic* milestones/decisions ambient misses.

Plain declarative sentences only — qwen2.5:14b extracts entities + relationships
from each, so phrasing matters.

Run:  python scripts/remember_session.py
"""

from __future__ import annotations

import asyncio

from friday.memory.store import get_store

# Session of 2026-06-03/04 — gap-closing + voice latency + Gemma 4 evaluation.
FACTS: list[str] = [
    # --- capabilities now working ---
    "Sutra's web search works through a self-hosted SearXNG container on port 8080.",
    "Sutra's router uses Groq's llama-3.3-70b as the MEDIUM tier and Google's "
    "gemini-2.5-flash as the HEAVY tier, both verified working at zero cost.",
    "Sutra's model fallback chain works: when one provider fails the router falls "
    "through to the next, and free local or API models are never charged.",
    # --- voice ---
    "Sutra's voice mode in VAD mode used to take over ten seconds to reply; it was "
    "fixed with an energy gate and now replies in about one second.",
    "Sutra's speech-to-text uses the small.en Whisper model, about six times faster "
    "than large-v3 with the same quality on clear speech.",
    "Sutra's voice loop runs gemma3:4b locally as the fast voice handler.",
    # --- Gemma 4 evaluation (the current decision) ---
    "Sarjan is upgrading Sutra's local model to Gemma 4, because Gemma 4 is smarter.",
    "Sutra uses gemma4:e4b for the fast voice tier because it fits the 16 GB GPU "
    "and stays fast; it downloaded successfully at 9.6 GB.",
    "The gemma4:26b model is a mixture-of-experts with 26 billion total parameters but "
    "only about 4 billion active, so it is fast if it fits in VRAM.",
    "The gemma4:26b nvfp4 build cannot run on Sarjan's Linux machine because Ollama "
    "reports it requires macOS, so only gemma4:e4b was usable on this box.",
    # --- the principle Sarjan set ---
    "Sutra's memory graph is the single source of truth: everything Sutra or its "
    "assistant needs should be searchable there to save tokens and time.",
]


async def main() -> None:
    store = get_store()
    if not await store.initialize():
        print("✗ memory store unavailable — is FalkorDB running? "
              "(sudo docker start sutra-falkordb)")
        return
    print(f"Remembering {len(FACTS)} session facts (qwen2.5:14b extraction — slow)…\n")
    for i, fact in enumerate(FACTS, 1):
        ok = await store.remember(fact, source="session")
        print(f"  {'✓' if ok else '✗'} [{i}/{len(FACTS)}] {fact[:70]}…")
    await store.close()
    print("\nDone. These are now recallable:  sutra recall \"what did we change in voice\"")


if __name__ == "__main__":
    asyncio.run(main())
