# Sutra (friday-os) — Claude Code Context

## Read first
- `docs/FRIDAY_FINAL_SPEC.md` — master spec, every locked decision
- `docs/FRIDAY_VISUAL_DIRECTION.md` — GUI direction (Month 3)
- `docs/FRIDAY_SKILLS_RESEARCH.md` — self-extending skills research

## What this is
Voice-native ambient AI assistant. JARVIS-class. Sarjan (Sudbury, Ontario) is the sole developer.
v1 target: ~6 months at 10 hrs/wk.

## Hardware
- Kali Linux 2026.2 (kali-rolling), X11 session
- RTX 3080 Ti Mobile — 16 GB VRAM (nvidia-smi not installed yet, drivers pending)
- 32 GB RAM, 90 GB free disk

## Stack
- Router: LiteLLM tiered (local Ollama → Groq → Gemini → Claude)
- Council: Claude API (Sonnet default, Opus rare, Haiku for cheap)
- Local: gemma3:4b (fast voice/chat, no tools), qwen2.5:7b (offline FUNCTION-CALLING fallback — quota-proof tool turns), gemma4:e4b (reasoning), qwen2.5:14b (memory extraction)
- Voice: faster-whisper Large-v3 INT8 + Kokoro + Pipecat (Week 2)
- Memory: Graphiti + FalkorDB + Qdrant (Week 3)
- Desktop automation: xdotool (X11 confirmed)
- Search: SearXNG self-hosted

## Project structure
```
friday/
  core/
    router.py     # LiteLLM tiered routing, budget tracking
    agent.py      # Tool-call loop
  skills/
    registry.py   # @skill decorator, JSON schema gen, autodiscovery
    web_search.py # SearXNG
    file_ops.py   # read/write/list
    system_info.py # CPU/RAM/disk/GPU stats + run_command
  cli.py          # Rich REPL — sutra chat / sutra ask / sutra skills
docs/             # Planning docs
tests/            # pytest-asyncio
```

## Code standards
- Python 3.11+, async everywhere, type hints required
- pydantic v2 for validation
- keyring for secrets (never .env for secrets)
- Confirmation policy: auto_confirm=True for reads, False for irreversible writes
- No files >200 lines if avoidable

## Current state (Week 1)
Foundation complete: router, skills registry, 3 skills, agent loop, CLI.
Next: install deps, set API keys, smoke test. Then Week 2 (voice).

## Install
```bash
cd ~/friday-os
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,search]"
cp .env.example .env  # add API keys
sutra chat
```
