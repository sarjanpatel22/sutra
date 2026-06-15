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
- Brain routing: `core/autoroute.py` decides per-turn — `decide()` (act + effort + tier) and `chat_tier()` (substantive chat → free Groq 70B, trivial greetings → local gemma3:4b). Free by default; Claude only if `ROUTER_ALLOW_PAID=1`.
- Council: Claude API (Sonnet default, Opus rare, Haiku for cheap)
- Local: gemma3:4b (fast voice/chat, no tools), qwen2.5:7b (offline FUNCTION-CALLING fallback — quota-proof tool turns), gemma4:e4b (reasoning), qwen2.5:14b (memory extraction)
- Voice: faster-whisper Large-v3 INT8 + Kokoro + Pipecat (Week 2)
- Memory: Graphiti + FalkorDB; vectors via nomic-embed-text in an in-process numpy cosine recall index (Qdrant dropped — see docs/SUTRA_PHASES.md)
- Desktop automation: xdotool (X11 confirmed)
- Screen vision: ImageMagick `import` (capture) + tesseract (OCR → real click coordinates) + free Groq vision (Llama-4-Scout)
- Search: SearXNG self-hosted

## System tools (installed on this Kali box, no sudo)
- `import` (ImageMagick) — desktop screenshots: `vision/screen.py` (downscaled JPEG + 8x8 average-hash change detection) and `vision/locate.py` (full-res PNG for OCR)
- `tesseract` — OCR; `vision/locate.py` reads on-screen text to real bounding boxes (click centre)
- `xdotool` — moves the mouse / clicks / types for the desktop take-over skills

## Project structure
```
friday/
  core/
    router.py     # LiteLLM tiered routing, budget tracking
    autoroute.py  # per-turn brain — decide() + chat_tier()
    agent.py      # Tool-call loop (SUTRA_AUTONOMOUS unlocks hands-free gated skills)
  skills/
    registry.py   # @skill decorator, JSON schema gen, autodiscovery
    web_search.py # SearXNG
    file_ops.py   # read/write/list
    system_info.py # CPU/RAM/disk/GPU stats + run_command
    guide.py      # guide_me — looks at the screen, says which control to click (read-only)
    desktop_control.py # act_on_screen / type_into — OCR-locate + xdotool click/type (gated)
  vision/
    screen.py     # grab_jpeg + average-hash change detection
    locate.py     # tesseract OCR → on-screen text + real coordinates
  watch/          # sutra watch — ambient loop (loop/suggest/state)
  cli.py          # Rich REPL — sutra chat / ask / skills / guide / do / watch
docs/             # Planning docs
tests/            # pytest-asyncio
```

## Code standards
- Python 3.11+, async everywhere, type hints required
- pydantic v2 for validation
- keyring for secrets (never .env for secrets)
- Confirmation policy: auto_confirm=True for reads, False for irreversible writes
- No files >200 lines if avoidable

## Current state
Foundation through web/system actions shipped: router, skills registry, agent loop, CLI, voice, memory, MCP bridge, dashboard. ~43 skills across ~26 modules (`sutra skills` lists them).
JARVIS upgrade landed (2026-06-14):
- **Brain** — `autoroute.chat_tier()` routes substantive chat to the free Groq 70B, keeps trivial greetings on local gemma3:4b; used on the voice/chat non-action paths.
- **`sutra watch`** — `friday/watch/`: ambient loop, screenshots only when the screen changes, asks a free vision model for ONE genuinely-useful suggestion (silent otherwise) via desktop notification. OFF by default; `--once` flag; tuned by `WATCH_*` env. Privacy: changed screenshots go to Groq's vision API.
- **`guide_me`** (`sutra guide "<goal>"`) — looks at the real desktop and says which control to click. Read-only.
- **`act_on_screen` + `type_into`** (`sutra do "<goal>"`) — the take-over: OCR-finds a text-labelled control (tesseract, real coords) and clicks/types via xdotool. IRREVERSIBLE, confirmation-gated (`-y`/`--yes` to skip); hands-free only under `SUTRA_AUTONOMOUS`. Limit: text-labelled controls only (icon-only buttons aren't found).

## Install
```bash
cd ~/friday-os
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,search]"
cp .env.example .env  # add API keys
sutra chat
```
