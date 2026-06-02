# Friday OS — Reality Check & v1 Scope

> Companion to `FRIDAY_HANDOVER.md`. Read both.
> The handover is the **full vision**. This doc is the **shippable plan**.

---

## TL;DR

The vision in the handover is achievable. The 4–6 month timeline is not.
Realistic path: ship **v1 in 3 months** (voice + memory + skills + basic
dashboard), then iterate v2/v3 over the following 12+ months. Everything in
the handover still ships — just not all at once.

---

## What the original plan got right (don't change)

- LiteLLM router pattern
- Decorator-based skill registry as the heart of the system
- Local-first for STT / TTS / embeddings / routing; cloud for heavy reasoning
- FalkorDB over Neo4j
- Graphiti for memory
- SearXNG over Google API
- Tauri over Electron
- n8n as integrations hub, NOT main brain
- Avatar path A+D (wireframe + particles), not photorealistic
- Sub-15-word voice responses by default
- No chat box in the GUI — voice + generative UI

---

## What was wrong / under-scoped

### 1. Timeline

4–6 months part-time for 9 phases is fantasy. Realistic:
- 3–4 months → v1 (phases 1–5 done well)
- +12 months → 90% of the full vision
- +24 months → full vision including avatar fidelity

### 2. Avatar (original Phase 6)

The wireframe-holographic-particle aesthetic with audio-reactive lip sync,
emotion engine, idle dissolve — that's a 6+ week project on its own for a
graphics specialist. Solo + part-time + on top of everything else = it
becomes the thing that eats the project.

**v1 decision**: ship an audio-reactive particle orb. Defer the wireframe
head to v2.

### 3. Self-extending skills (original Phase 8)

Voyager worked in Minecraft because success was verifiable in-engine.
"Friday writes a skill, tests in Docker, registers if passes" — what's the
pass criterion for an arbitrary skill? Unsolved research problem.

**v1 decision**: manual skill writing with Claude's help, human review every
line. Defer autonomous skill learning.

### 4. Proactive layer (original Phase 9)

Signal-to-noise is the hardest unsolved problem in AI assistants. Either
annoying or silent. Needs months of behavioral tuning.

**v1 decision**: deferred entirely. v1 is reactive.

### 5. WhatsApp / Instagram automation

Baileys gets accounts banned. Instagram fights bots aggressively. These
break monthly.

**v1 decision**: not in v1. v2 add-ons with the understanding they'll be
unreliable.

### 6. Always-on screen awareness

Continuous OCR/vision on screen content is expensive and the value vs.
"just ask" is unclear.

**v1 decision**: defer until you know what you actually want from it.

### 7. Multi-device sync

CRDT or eventual consistency for memory graph across desktop/phone/server
is non-trivial. Single-device must be rock solid first.

**v1 decision**: single-device for first 2 months, Telegram bridge in
month 3.

---

## Brain / LLM stack — reality check

### Verified

| Layer | Pick | Status |
|-------|------|--------|
| High council | Claude | ✓ Correct for hardest reasoning |
| Fast tier | Groq Llama 3.3 70B | ✓ Good, but free tier rate-limits at daily-driver volume |
| Free middle | Gemini free tier | ✓ Useful, also rate-limited |
| Local router | Gemma 4 E4B | ✓ Real (released April 2026), native function calling + audio input, 128K context — perfect router. ~3.5GB VRAM at Q4 |
| Local architect | Qwen 2.5 Coder 32B | ✗ **Won't fit.** Q4 ≈ 19GB on 16GB VRAM. Drop to Qwen Coder 14B (~9GB, fits with swap) or send architect-tier to cloud |

### VRAM math (16GB total, voice-mode on)

```
Gemma 4 E4B router       always loaded   ~3.5 GB
Whisper Large-v3         always loaded   ~3.0 GB
Kokoro TTS               always loaded   ~1.0 GB
Embeddings (Qdrant)      always loaded   ~0.5 GB
                                         -------
                                          ~8.0 GB burnt
                                          ~8.0 GB free for one on-demand model
```

**Conclusion**: heavy reasoning must be cloud. Local handles voice +
routing + memory only. Clean split.

### Budget reality

$0–20/month is optimistic for actual daily-driver usage. Plan for
**$30–80/month** in Claude API. Set hard ceiling in LiteLLM, fallback
chain:

```
Claude (with monthly cap)  →  Groq  →  Gemini  →  Local Gemma 4
```

Keeps cost predictable. Degrades gracefully when caps/limits hit.

---

## JARVIS feasibility

| JARVIS trait | v1 reachable | Notes |
|--------------|--------------|-------|
| Always there | ✓ | wake word + multi-device |
| Speaks naturally | ✓ish | Kokoro is good, you'll still hear it's TTS |
| Knows your history | ✓ | Graphiti nails this |
| Controls your stuff | ✓ | computer / web / smart home yes |
| Pair-codes | ✓ | solved space (Aider / Continue / Cline patterns) |
| Multi-step planning | ✓ | Claude handles this |
| Personality | ✓ | system prompt + memory |
| Visual presence | ⚠️ | particle orb in v1, wireframe head in v2 |
| Proactive | ⚠️ | deferred to v2+, hardest unsolved problem |
| Self-extending skills | ⚠️ | deferred. Manual + Claude-assisted gets 90% of the value |

Net: ~80% of what makes JARVIS *feel* like JARVIS is reachable in v1.
The remaining 20% is years of work.

---

## The three honest paths

### Path 1 — v1 first → iterate (recommended)

3 months to a daily-useful Friday, then iterate v2/v3 over 12+ months.
**Lowest abandonment risk.** Something works at month 1.

### Path 2 — full grind (18–24 months)

Build everything in the handover in original order. No usable Friday until
~month 8. **High abandonment risk.** Only viable if commitment is bulletproof.

### Path 3 — cut nothing, ship 4–6 months

Not possible at production quality. Janky demo of everything, breaks
constantly. **Worst outcome.**

---

## V1 roadmap (3 months)

### Month 1 — Foundation (text + voice + memory)

| Week | Build | Unlocks |
|------|-------|---------|
| 1 | Router + skill registry + 4 skills (web, file ops, system info, calc) + Rich CLI | Talk to Friday in terminal, real work gets done |
| 2 | Voice loop: Whisper + Kokoro + Pipecat, push-to-talk | Voice-driven, single device |
| 3 | Graphiti + FalkorDB + episodic memory + entity extraction | She remembers people, projects, conversations |
| 4 | Daily-driver shakedown — kill latency, kill friction | Replaces real daily tasks |

**Exit criteria**: voice-driven assistant with memory, does web/file/system
work. Beats 90% of "AI assistants" out there.

### Month 2 — Capability

| Week | Build | Unlocks |
|------|-------|---------|
| 5 | Playwright + browser-use skills | Browses and acts on websites |
| 6 | Open Interpreter + IDE bridge (Continue.dev pattern) | Runs commands, edits code |
| 7 | Dashboard MVP: Tauri + Next.js, 5 widgets (memory graph, task state, file/code preview, web cards, settings) | Useful GUI |
| 8 | Wake word + always-listening + interruptible | Ambient mode works |

### Month 3 — Identity

| Week | Build | Unlocks |
|------|-------|---------|
| 9 | Audio-reactive particle orb (NOT the wireframe head yet) | Visual presence |
| 10 | Generative UI: Claude picks widgets per query, streams in via Vercel AI SDK | Dashboard reshapes per question |
| 11 | Telegram bot + state sync to home server | Multi-device |
| 12 | Observability (logs, traces) + bug-fix weekend | Stable daily driver |

**Exit criteria** = **v1 ships**: voice-native, ambient, with memory,
web/file/system/code capable, generative dashboard, phone access.

---

## v2 territory (months 4–9, only after v1 is stable)

- Wireframe holographic avatar — own 6-week project
- Self-extending skills — needs verifiable test harness first
- Proactive layer — needs behavioral data + 2 months tuning
- WhatsApp / Instagram automation — unreliable add-ons
- Always-on screen awareness — expensive, define use case first
- Voyager-style autonomous skill learning from GitHub
- Architect sub-agent (Qwen Coder local or Claude)
- Watchdog sub-agent (continuous monitoring including Wazuh integration)

---

## Hard rules for v1

- Don't autonomously learn skills — write them with Claude, review every line
- Don't try to be proactive — reactive is hard enough
- Don't optimize the avatar before the brain works
- Don't add a feature that doesn't earn its keep daily
- Don't add multi-device until single-device is rock solid
- Every milestone must result in something you *actually use that day* —
  no infrastructure-only phases

---

## Architecture (unchanged from handover)

```
User (voice / text / camera / screen)
   ↓
Router (Gemma 4 E4B locally — intent classification)
   ↓
   ├── Simple   → handled directly
   ├── Medium   → Groq Llama 3.3 70B
   └── Complex  → Claude (high council)
              ↓
         Claude plans, delegates sub-tasks, synthesizes:
            - voice_response   (Kokoro speaks it, orb pulses)
            - layout_spec      (which widgets to render)
            - tool_calls       (skills to execute in parallel)
   ↓
Tools run → stream results back → widgets mount
```

**Voice-mode latency note**: skip router for voice, let one model handle
in-context. Router pattern adds 200–500ms per hop; voice needs <800ms total.
Use the high council only for explicit complex requests.

---

## Confidence / confirmation pattern (new, not in original)

Not in the original handover but critical: when Friday takes irreversible
actions (push code, send message, delete file, schedule meeting), she must
**confirm before acting** unless the user explicitly pre-authorized.

A 95%-reliable system that occasionally takes the wrong action is worse than
no system. Design confirmation thresholds *in*, not bolted on after.

Pattern:
- **Read actions** (search, fetch, summarize) → no confirmation
- **Reversible writes** (draft, save scratch file) → no confirmation
- **Irreversible writes** (send, push, delete, post, purchase) → always confirm
- **User can override per skill** via `@skill(auto_confirm=True)` decorator

---

## Pre-code checklist

Answer before writing the first Python file:

1. **GPU model** → `nvidia-smi`
2. **Session type** → `echo $XDG_SESSION_TYPE`  *(Wayland or X11 — affects automation tools)*
3. **Distro** → `cat /etc/os-release`  *(affects systemd setup)*
4. **Phase 1 order** → serial (CLI working before voice) recommended
5. **Claude API monthly cap** → set in `.env` (suggest start at $30)
6. **Path choice** → 1, 2, or 3 from above

---

## Code order for week 1

```
friday/core/router.py         # LiteLLM tiered routing + fallback chain
friday/skills/registry.py     # @skill decorator + auto-discovery
friday/skills/web_search.py   # First skill, hits SearXNG container
friday/core/agent.py          # Tool-call loop, intent → model → skills → answer
friday/cli.py                 # Rich-based REPL, conversation history
```

One sitting if possible. End of day 1: type a question, watch Friday route
to a model, call SearXNG, answer back.

---

## What this doc is NOT

- Not a replacement for `FRIDAY_HANDOVER.md` — that's the vision
- Not the full architecture — see handover for sub-agents, Wazuh integration,
  end-state walkthrough
- Not permanent — revisit at end of month 1 and month 2 to re-scope based on
  what actually worked
