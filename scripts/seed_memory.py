"""Seed the memory graph with real facts about Sarjan and this machine.

Gathers live OS/hardware facts straight from the running system and writes them,
along with profile facts, as natural-language episodes into the Graphiti/FalkorDB
store. Graphiti's LLM extraction turns each sentence into entities + relationships,
so phrasing matters — keep facts as plain declarative sentences.

Run:  python scripts/seed_memory.py
This is the "real memory" starter set; the graph grows further as Sutra runs.
"""

from __future__ import annotations

import asyncio
import platform
import shutil
import socket
import subprocess

import psutil

from friday.memory.store import get_store


def _gpu() -> tuple[str, int] | None:
    """(name, VRAM in MB) from nvidia-smi, or None if no GPU / driver present."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if not out:
            return None
        name, mb = out.splitlines()[0].split(",")
        return name.strip(), int(mb)
    except Exception:  # noqa: BLE001 — no GPU / driver: just skip the fact
        return None


def gather_facts() -> list[str]:
    """Build the list of natural-language episodes to remember."""
    host = socket.gethostname()
    kernel = platform.release()
    try:
        import distro
        os_name = distro.name(pretty=True)
    except Exception:  # noqa: BLE001
        os_name = f"{platform.system()} {platform.release()}"
    cores = psutil.cpu_count(logical=True)
    ram_gb = round(psutil.virtual_memory().total / 1e9)
    u = shutil.disk_usage("/")
    disk_total = round(u.total / 1e9)

    facts = [
        # --- profile ---
        "Sarjan is from Sudbury, Ontario, Canada.",
        "Sarjan is a software engineer at Acme Corp.",
        "Sarjan is building Sutra, a voice-native ambient AI assistant.",
        # Atomic restatement: extraction drops adjectives buried in the compound
        # sentence above ("voice-native" was lost), so state the descriptor alone.
        "Sutra is a voice-native ambient AI assistant.",
        # --- this machine (live values) ---
        f"Sarjan's computer is named {host} and runs {os_name} on Linux kernel {kernel}.",
        f"Sarjan's machine has {ram_gb} GB of RAM, a {cores}-core CPU, "
        f"and a {disk_total} GB disk.",
        # --- the system Sarjan is building ---
        "Sutra's memory layer is built on Graphiti and FalkorDB, with Qdrant for vectors.",
        "Sutra runs Ollama locally, using qwen2.5:14b for memory extraction "
        "and gemma3:4b as the fast voice handler.",
        "Sutra's voice loop uses faster-whisper for speech-to-text and Kokoro for "
        "text-to-speech, accelerated on the GPU.",
    ]
    gpu = _gpu()
    if gpu:
        name, vram_mb = gpu
        # Two atomic facts — a compound "…3080 Ti, 16384 MB GPU" loses the VRAM
        # value in extraction; split so name and VRAM each survive as their own edge.
        facts.append(f"Sarjan's GPU is an {name}.")
        facts.append(f"Sarjan's GPU has {round(vram_mb / 1024)} GB of VRAM ({vram_mb} MB).")
    return facts


async def main() -> None:
    store = get_store()
    if not await store.initialize():
        print("✗ memory store unavailable — is FalkorDB running?")
        print("  start it with:  sudo docker start sutra-falkordb")
        return

    facts = gather_facts()
    print(f"Seeding {len(facts)} episodes into the memory graph (qwen2.5:14b extraction)…\n")
    for i, fact in enumerate(facts, 1):
        ok = await store.remember(fact, source="seed")
        mark = "✓" if ok else "✗"
        print(f"  {mark} [{i}/{len(facts)}] {fact}")
    await store.close()
    print("\nDone. Open the graph:  sutra graph")


if __name__ == "__main__":
    asyncio.run(main())
