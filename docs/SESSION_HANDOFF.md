# Sutra — Session Handoff (2026-06-04)

Pick-up doc for the next session. Captures current state, everything done, the
non-obvious gotchas, and open threads. Pairs with `docs/MODERNIZATION_AUDIT.md`
(stack vs 2026 SOTA) and `docs/MEMORY_VIEWER_AND_AMBIENT.md`.

---

## TL;DR — current state
Sutra is **healthy, fast, audited end-to-end**. 66 tests green, `main` synced with
`origin/main` (tip `1fffba6`). This session fixed ~15 real bugs, made text chat
**27s → instant**, cured voice fragmentation, made memory queryable + self-healing,
and audited every module. The architecture is sound (validated against SOTA);
the bugs were in *wiring and robustness*, not design.

**The one live gap:** voice works synthetically + composed live, but the **laptop
mic is too far-field** (low SNR → Whisper hallucinates). Fix = a close mic/headset.
Not a code issue.

---

## Model lineup — who does what
| Job | Model | Tier / entry |
|-----|-------|--------------|
| 🎙️ Voice (instant) | `gemma3:4b` (Ollama) | `Tier.LOCAL`, `sutra voice` |
| 🧠 Hard reasoning | `gemma4:e4b` (Gemma 4, **thinking on**) | `Tier.REASON`, **`sutra think "..."`** |
| ☁️ Free cloud | `groq/llama-3.3-70b` → `gemini/gemini-2.5-flash` | `Tier.MEDIUM` |
| 💪 Heavy | `claude-sonnet-4-6` → groq fallback | `Tier.HEAVY` (no ANTHROPIC key set = opt-in) |
| 🗣️ STT | `distil-large-v3` faster-whisper (~76ms warm) | `WHISPER_MODEL` |
| 🔊 TTS | Kokoro `bm_george` (UK male) | `KOKORO_VOICE` |
| 🎚️ VAD | **Silero** neural ONNX (CPU) | `friday/voice/vad.py` |
| 🕸️ Memory extraction | `qwen2.5:14b` (Ollama) | Graphiti |
| 🔢 Embeddings | `nomic-embed-text` (768-d, 29ms) | `EMBED_MODEL` |

**Gemma 4 is NOT broken** — it's a *reasoning* model (emits ~300 thinking tokens
before answering). Wrong for instant voice (and 9.6GB vs 3.3GB), right for `sutra
think`. Voice passes `think=False` so reasoning models don't return empty there.

---

## What was fixed/built this session (all pushed)
**Voice**
- `webrtcvad → Silero VAD` — killed the speech fragmentation (the big one). Energy-gate VAD was a stopgap; Silero is the real fix.
- STT hot path ~30% faster: `vad_filter=off` (Silero already trims), `language=en` (skip detection), `beam=1`. Wired `WHISPER_DEVICE/LANGUAGE/BEAM`.
- Mic crash fix (`AUDIO_INPUT_DEVICE=pulse`), LLM prewarm, identity anchor (recalled "Sarjan" facts → "you"), async voice persist (source="voice"), `VAD_SILENCE_S` tunable.
- `KOKORO_VOICE` was dead config → wired (54 voices).

**Agent / router**
- **TEXT CHAT BLOCKED ~18s**: `run()` awaited the qwen2.5 persist before returning → now fire-and-forget (`persist_turn_async` + `flush_persist`). `sutra ask` 27s → instant.
- **Tool-JSON leaked as the answer** on local tier → tools only on cloud tiers (`LOCAL/REASON` get `tools=None`).
- Added `Tier.REASON` + `agent.reason()` + `sutra think` (Gemma 4, thinking on, own prompt `REASON_SYSTEM_PROMPT`, returns answer + reasoning trace, $0).
- Groq + Gemini wired & verified $0; fixed stale Gemini model (`gemini-2.5-flash`); fallback chain proven.

**Memory**
- **Recall was silently dead** — Graphiti wrote to graph `sarjan` but searched `default_db`. Fix: `FalkorDriver(database=_GROUP_ID)`. + keyword fulltext fallback.
- **`forget` hardened** — preview-by-default (`apply=False`); semantic forget over-deletes (it bit us twice — preview saved us both times). Never auto-deletes.
- Recall wired into voice; voice nodes colored sky-blue in the graph viewer.
- Store `reset()` + daemon reconnect — ambient survives a FalkorDB restart now.

**Robustness / infra**
- SearXNG stood up (`infra/searxng/`, container `sutra-searxng:8080`) — `web_search` works.
- Watchdog crash-proof (`_safe` wrapper) + honest (Redis/Qdrant = WARN "optional, not yet wired", not FAIL).
- Safety tests: budget typo guard, seeder idempotency.

**Full-codebase audit** — every module deep-read: voice, agent/router, memory store, ambient (daemon/collectors/config), monitor, registry. Privacy guardrails validated (denylist `.ssh/.aws/.env`, shell stores command-names only).

---

## Key gotchas & lessons (the non-obvious stuff)
- **Silero VAD ONNX needs a 64-sample context window** prepended to the 512 chunk (576 total). Bare 512 → returns ~0 for everything.
- **Graphiti+FalkorDB**: writes route by `group_id` graph, search reads `driver._database` — must set `database=group_id` or recall is dead.
- **Recall-quality issues here are DATA problems** (stale/conflicting facts), not the embedder. A/B proved nomic already ranks correctly; Qwen3-embedding investigated → **not worth it** (+98ms voice recall for ~no gain). Keep nomic.
- **`forget` is dangerous** (semantic over-match) — always preview first.
- **Gemma 4 / Qwen3 return "empty" via litellm streaming** because they're reasoning models hitting the token cap on thinking → use `think=False` for non-reasoning use.
- **Recurring bug pattern**: documented-but-unwired env vars (`WHISPER_MODEL`, `WHISPER_DEVICE`, `KOKORO_VOICE` all were dead config). When adding an env knob, verify it's actually read.
- **`pkill -f "sutra voice"` self-matches** and exits 144 — use `[ ]` bracket class, and don't chain it with the launch in one command.
- **streaming STT is marginal for us** (STT already 76ms warm) — don't bother.
- **Live mic gibberish = SNR/mic-distance, not noise floor** (loud speech + the same room noise transcribes perfectly). RNNoise can't fix quiet far-field speech. Close mic is the fix. (pyrnnoise = buggy + 100MB bloat, avoid.)

---

## Services / infra
- ✅ Running: FalkorDB (`sutra-falkordb:6379`), SearXNG (`sutra-searxng:8080`), Ollama
- ⚠️ Down but OPTIONAL (not wired into code): Redis (6380), Qdrant (6333) — `docker start sutra-redis sutra-qdrant` if ever needed
- Docker needs sudo (not in docker group); password was pasted in chat — **should be rotated** (`passwd`); treat `Sarjan@123` as compromised.
- HW: RTX 3080 Ti Mobile 16GB VRAM, 32GB RAM, Kali Linux, X11 (`DISPLAY=:0`)

## How to run
```bash
sutra chat                  # text REPL (instant now)
sutra ask "..."             # one-shot
sutra think "..."           # Gemma 4 local reasoning (--show for trace)
sutra voice                 # push-to-talk (best for the far-field mic)
sutra voice --mode vad      # hands-free (Silero)
sutra graph                 # brain-graph viewer :8700
sutra ambient               # OS-memory daemon
python scripts/seed_memory.py            # reseed profile/OS facts
python scripts/remember_session.py       # store session milestones
```

---

## Open threads (pick up here)
1. **Voice live**: pipeline verified; needs a **close mic/headset** for clean SNR. Plug one in → set `AUDIO_INPUT_DEVICE` to it → re-test. Or use push-to-talk + speak close.
2. **The GUI / particle orb** — Tauri + Next.js + Three.js (Month-3 vision, nothing built yet). The big exciting arc.
3. Lower-priority audit roadmap: barge-in/interruption, Chatterbox-Turbo TTS (expressive). Embeddings & streaming-STT investigated → not worth it.

**Sarjan**: developer, from Sudbury Ontario. Code-first,
production standards, no hand-holding. Values measure-before-changing rigor;
GitHub pushes need explicit go.
