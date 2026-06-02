# Friday OS — Final Spec (v1)

> **This is the master doc.** Supersedes `FRIDAY_HANDOVER.md` and
> `FRIDAY_REALITY_CHECK.md`. Every number here is verified or explicitly flagged.
> Last revised after full architectural and budget review.

---

## 0. TL;DR

A voice-native ambient personal assistant, JARVIS-class in feel, built on a
16GB VRAM laptop in Toronto. v1 ships in **6–9 months at sustainable pace**
(5–10 hrs/wk after a full-time SWE job) or **3 months at 20 hrs/wk grind**.
Local-first for STT/TTS/routing/memory, Claude API for hard reasoning. Realistic
total cost **$40–100/mo**, grindable down to **~$25/mo** with prompt caching
and tight routing.

---

## 1. Identity (still open — decide before code)

| Option | Verdict |
|--------|---------|
| Friday | Original. Marvel-cliché, but recognizable. |
| Sutra (सूत्र) | Indian mythological, maps cleanly to graph memory (threads of knowledge). **Wake-word acoustically strong** — sibilant `/s/` is highly distinctive against ambient noise. |
| Astra (अस्त्र) | Indian mythological, sharper. **Wake-word acoustically weaker** — open vowel `/a/` start, harder to detect cleanly. |

**Recommendation: Sutra.** Best wake-word acoustics + thematic fit with the
graph-memory architecture. (The previous "Astra has phonic sharpness" claim was
incoherent — Whisper isn't the wake-word detector, openWakeWord is, and `/s/`
beats `/a/` for openWakeWord every time.)

The codebase repo stays `friday-os` regardless — name the assistant, don't
rename the directory.

---

## 2. Vision (locked from original handover)

Voice-native ambient presence. Not a chatbot. Holographic avatar (deferred to
v2 — orb in v1). Generative UI that reshapes per query. Graph memory. Truly
capable across web, files, system, code. Self-extending (deferred to v2).
Proactive (deferred to v2). Multi-instance (Telegram bridge in v1, full sync
in v2).

The line: **intention → outcome, with Friday as the invisible layer that
makes it happen.**

---

## 3. Hardware baseline

| Component | Spec | Notes |
|-----------|------|-------|
| OS | Linux (distro TBD) | Run `cat /etc/os-release` |
| RAM | 32 GB | Sufficient |
| VRAM | 16 GB | Constrains model choices below |
| GPU | TBD (likely NVIDIA) | Run `nvidia-smi` |
| Session | TBD | Run `echo $XDG_SESSION_TYPE` (Wayland vs X11 affects automation tools) |
| Location | Toronto | Network latency to Anthropic ~50–100 ms |
| LAN extras | Wazuh dashboard at 192.168.23.129 | v2 integration |

---

## 4. The stack (final picks)

### Brain layer

| Layer | Pick | Why |
|-------|------|-----|
| Cloud council (heavy reasoning) | **Claude API** | Sonnet default, Opus for hardest, Haiku for cheap tier |
| Cloud free tier (medium) | Groq (Llama 3.3 70B), Gemini 2.5 Pro/Flash | Fallback chain when Claude cap or budget hits |
| Local router + voice handler | **Gemma 4 E4B (Q4_K_M)** | Confirmed real (April 2026 release). Native function calling + native audio input. 128K context. ~4 GB VRAM. *Doubles as the voice path — no separate router step in voice mode.* |
| Local architect / terminal | **Qwen 3.5 9B (Q4_K_M)** | Confirmed real (Feb-Mar 2026 release). Multimodal (vision in for "look at this"), native tool calling, thinking mode. ~5.5 GB VRAM. **Note**: generalist, not a specialized coder — fine for sysadmin/terminal/light coding, weaker than DeepSeek Coder or Qwen 3 Coder on pure code tasks. |
| Routing infrastructure | **LiteLLM** | Tiered routing + fallback chain |

### Voice layer

| Component | Pick | VRAM | Notes |
|-----------|------|------|-------|
| STT | faster-whisper Large-v3 (INT8 via CTranslate2) | ~2 GB | Or `Whisper-Live` wrapper for streaming |
| TTS | Kokoro (consider Kokoro-ONNX for speed) | ~0.5 GB | 82M params. Phoneme alignment for lip-sync needs forced aligner on top — don't skip this for v2 avatar. |
| Wake word | openWakeWord | ~0.1 GB | Train custom "Hey Sutra" model |
| Voice pipeline | Pipecat | — | MIT-licensed, real-time |

### Memory layer

| Component | Pick | Notes |
|-----------|------|-------|
| Graph memory | **Graphiti** | Temporally-aware, multi-tenant |
| Graph DB | **FalkorDB** | **Now the default backend for Graphiti** (confirmed). 496× faster P99 than Neo4j on Graphiti's benchmarks. |
| Vector | Qdrant | For semantic search alongside graph |
| Embeddings | nomic-embed-text | 137M params, ~0.3 GB. Smaller than BGE-large, saves VRAM. |
| Structured | SQLite → Postgres if it grows | Personal data |

### Action layer

| Component | Pick | Notes |
|-----------|------|-------|
| Web automation | Playwright + browser-use | |
| System control | Open Interpreter | Sandboxed |
| Desktop input | ydotool (Wayland) / xdotool (X11) | Determined by session type |
| Workflows | n8n self-hosted (Docker) | **Integrations only, not the brain** |
| Search | SearXNG self-hosted | No rate limits, private |

### Backend

Python 3.11+ / FastAPI / asyncio / Redis. Standard.

### Frontend (v1 minimal)

Next.js 14 + Tailwind + shadcn/ui + assistant-ui + Vercel AI SDK (`streamUI`
for generative UI). Wrapped in Tauri. MapLibre for maps. Framer Motion for
transitions. Three.js for the visual presence.

### Visual presence

- **v1**: Audio-reactive particle orb (Three.js, custom shaders, ~25–30 hrs work)
- **v2**: Wireframe holographic head with TalkingHead.js base, Kokoro phoneme
  alignment for lip sync, emotion engine, idle dissolve. **6+ weeks of work
  on its own.** Do not start this until v1 is stable.

### Borrow from

Continue.dev / Aider / Cline for the IDE bridge and coding agent patterns.

---

## 5. VRAM math (verified — physics, not opinion)

16 GB total. **Always-loaded baseline** (voice mode on, memory active):

```
faster-whisper Large-v3 (INT8)    2.0 GB
Kokoro TTS                        0.5 GB
Gemma 4 E4B Q4_K_M                4.0 GB    (with ~8K context KV cache)
Embeddings (nomic-embed)          0.3 GB
openWakeWord                      0.1 GB
                                  ──────
Always burnt                     ~6.9 GB
Free for on-demand               ~9.1 GB
```

**When Qwen 3.5 9B loads for architect tasks:**

```
Qwen 3.5 9B Q4_K_M                5.5 GB    (on-demand)
                                  ──────
Total loaded                    ~12.4 GB
Headroom                         ~3.6 GB    (KV cache + buffers)
```

**Verdict**: works. Tight but workable. Qwen 3.5 9B should be unloaded between
sessions (LiteLLM-managed) to keep the headroom at 9 GB when idle.

**What does NOT fit**: Qwen 2.5 32B at any quant alongside voice stack. Llama
3.3 70B locally (period). Plan accordingly.

---

## 6. Voice latency math (verified — physics, hard floors)

Round-trip from "user stops speaking" → "first audio reply":

**Local path (Gemma 4 E4B handles):**

| Stage | Time |
|-------|------|
| VAD end-of-speech | 500 ms (can't go below ~300 without cutting users off) |
| Whisper STT on 5s audio | 300 ms |
| Gemma 4 E4B time-to-first-token | 200 ms |
| Kokoro TTS first audio chunk | 150 ms |
| **Local floor** | **~1150 ms** |

**Cloud path (Claude handles):**

| Stage | Time |
|-------|------|
| VAD | 500 ms |
| Whisper STT | 300 ms |
| Network Toronto → Anthropic | 80 ms |
| Claude TTFT | 600 ms |
| Kokoro first chunk | 150 ms |
| **Cloud floor** | **~1630 ms** |

**Mitigations** (must implement, not optional):
- **Streaming TTS** — start speaking before LLM finishes generating
- **Aggressive but safe VAD** — tune to ~350 ms silence threshold
- **Short responses** — system prompt enforces sub-15-word default
- **Skip cloud for simple queries** — Gemma 4 E4B handles ~70% of voice locally

JARVIS-snappy back-and-forth (under 500 ms) is **not achievable**. Sub-second
on local path is the practical ceiling.

---

## 7. Budget math (verified)

### Claude API at daily-driver usage

Assumption: 50 complex queries/day → Claude. 8K input + 500 output per query.

| Model | Cost per query | Monthly |
|-------|---------------|---------|
| Claude Haiku | $0.008 | ~$12 |
| Claude Sonnet | $0.032 | ~$48 |
| Claude Opus | $0.158 | ~$237 |

### Other monthly costs

| Item | Cost |
|------|------|
| Electricity (GPU ~12 hr/day at +120W) | $3–5 |
| Twilio for SMS/voice skills (optional) | $1–5 |
| Cloud backup of memory graph (optional) | $1–5 |
| Tailscale (remote access) | $0 (free tier) |
| All self-hosted (SearXNG, n8n, FalkorDB, Qdrant, Whisper, Kokoro, Gemma, Qwen) | $0 |

### Total tiers

| Tier | What you get | Monthly |
|------|--------------|---------|
| **Aggressive minimum** | Tight routing, heavy prompt caching, Haiku-default, Opus only for hardest 5%, no extras | **$20–35** |
| **Comfortable daily driver** | Sonnet-default council, Opus for hardest 10%, all integrations | **$50–100** |
| **Heavy / Opus-tier reasoning daily** | Opus default, max integrations | **$200–300** |
| **$0 mode (Gemini free tier as council)** | Quality drops, rate-limited to ~4–5 hrs intensive use/day | **$0–5 electricity** |

### Cost reduction levers

- **Anthropic prompt caching**: up to **90% off cached input tokens**. For
  Friday this is huge — system prompt + memory context + active conversation
  are repeated across calls. Implement from day 1.
- **Tight routing**: route to Claude only when Gemma 4 E4B can't handle it.
  Realistic split: 70% local, 20% Groq/Gemini free, 10% Claude.
- **Haiku as default council, Sonnet as escalation**: cuts cost ~4×.

### One-time costs (if needed)

| Item | Cost |
|------|------|
| Decent mic (if you don't have one) | $50–150 |
| Mini PC for always-on home server | $300–500 |
| GPU upgrade to 24 GB+ if 16 GB chokes | $800+ |

---

## 8. Timeline reality (verified — math)

v1 = **~245 productive hours** of work. At a working SWE's sustainable pace:

| Hrs/week | v1 timeline |
|----------|-------------|
| 5 | ~12 months |
| 8 | ~7 months |
| 10 | ~6 months |
| 15 | ~4 months (you'd have to grind) |
| 20 | ~3 months (not realistic with FT job) |

Subtract 20–30% for context-reload overhead per session. **Honest target:
6–9 months for v1 at sustainable pace.**

The full handover vision (avatar + self-extending skills + proactive +
WhatsApp/Instagram/multi-device sync) = **2+ years** at the same pace.

This is not bad. It's how real things get built. Plan for milestones, not
weeks. When life eats a month, the plan doesn't break.

---

## 9. Architecture — the high council pattern (revised)

```
User input (voice / text / camera / screen / hotkey snapshot)
   │
   ▼
┌─────────────────────────────────────────┐
│ Gemma 4 E4B  (local, always loaded)     │
│  • Handles ~70% of queries directly     │
│  • Native function calling              │
│  • Native audio input (voice mode)      │
│  • Routes complex requests upward       │
└─────────────────────────────────────────┘
   │
   ├── Simple / direct → handled in-place
   ├── Medium → Groq Llama 3.3 70B (free tier) or Gemini Flash
   ├── Heavy code/sysadmin → Qwen 3.5 9B (local, on-demand load)
   └── Complex reasoning → Claude API (council tier)
              │
              ▼
   Claude plans, delegates sub-tasks back down,
   synthesizes final answer:
     • voice_response  (Kokoro speaks, orb pulses)
     • layout_spec     (which widgets to render)
     • tool_calls      (skills executed in parallel)
   │
   ▼
Tools run → stream results back → widgets mount and animate
```

**Key revision from original handover**: Gemma 4 E4B is **both** the router
**and** the voice handler. Voice mode does NOT do router→voice-model as two
hops. One model handles audio in, function calling, and audio-context
response. Saves 200–500 ms per turn.

### Fallback chain (LiteLLM)

```
Claude  →  Groq Llama 3.3 70B  →  Gemini 2.5 Pro/Flash free  →  Qwen 3.5 9B local
   │
   └── monthly cap hit / network down / rate-limited → next link automatically
```

**Survival mode**: if internet drops, agent stays 100% functional on Gemma 4
+ Qwen 3.5 + FalkorDB locally. Voice loop unaffected.

### Stateless agent + stateful memory service

Build the agent **stateless** from day 1. State lives in:
- FalkorDB (graph memory)
- Qdrant (vector memory)
- Redis (session + tool state)

This makes multi-device additive in v2 instead of a rewrite.

---

## 10. Skill registry pattern (the heart of the system)

```python
@skill(
    name="web_search",
    description="Search the web via local SearXNG",
    auto_confirm=True,         # read-only, no confirmation needed
    timeout_s=10,
)
async def web_search(query: str, max_results: int = 5) -> list[SearchResult]:
    ...
```

- Decorator auto-registers in the skill registry
- JSON schema auto-generated from type hints + docstring
- Schema exposed to LLM via standard tool-calling
- Skills are async, run in parallel where possible
- Each skill has a confirmation policy (see §11)

### Skill credentials / secrets architecture

**Decision**: OS keyring (`python-keyring`), not `.env`, not vault.

- Encrypted at rest by the OS
- Per-skill namespacing: `friday:skill:gmail:oauth_token`
- `.env` only for non-secret config (model names, URLs)
- Secrets pulled lazily at skill-call time, never logged

---

## 11. Confirmation patterns for actions

| Action class | Behavior |
|--------------|----------|
| **Read** (search, fetch, summarize, look at this) | Auto, no confirmation |
| **Reversible write** (draft email, save scratch file, stage commit) | Auto, no confirmation |
| **Irreversible write** (send email, push code, delete file, post publicly, purchase) | **Always confirm** before acting |
| **Override** | `@skill(auto_confirm=True)` per skill, user-controlled |

Voice-mode confirmations are sub-15-word: "Send the email to Mike? Yes/no."

A 95%-reliable system that occasionally takes the wrong irreversible action
is **worse than no system**. Design this in from day 1, not bolted on after.

---

## 12. v1 roadmap (12 weeks at 20 hrs/wk OR ~6 months at 10 hrs/wk)

### Month 1 — Foundation

| Week | Build | Exit criteria |
|------|-------|---------------|
| 1 | LiteLLM router, skill registry, 4 skills (web_search, file_ops, system_info, calc), Rich CLI | Type a question, get a real answer using a skill |
| 2 | Voice loop: faster-whisper + Kokoro + Pipecat, push-to-talk | Speak a question, get spoken answer |
| 3 | Graphiti + FalkorDB + episodic memory + entity extraction tuning | Mention a person/project, recall them next session |
| 4 | Daily-driver shakedown — fix latency, fix friction | Use Friday for real daily tasks for 5 days |

### Month 2 — Capability

| Week | Build | Exit criteria |
|------|-------|---------------|
| 5 | Playwright + browser-use skills | Friday can browse and act on a site you specify |
| 6 | Open Interpreter + IDE bridge (Continue.dev pattern) | Friday can run terminal commands and edit code |
| 7 | Dashboard MVP: Tauri + Next.js, 5 widgets (memory graph, task state, file/code preview, web cards, settings) | GUI launches, shows live state |
| 8 | openWakeWord + always-listening + barge-in interruption | "Hey Sutra" wakes her from idle |

### Month 3 — Identity

| Week | Build | Exit criteria |
|------|-------|---------------|
| 9 | Audio-reactive particle orb (Three.js + custom shader) | Orb pulses with her voice and yours |
| 10 | Generative UI (Vercel AI SDK `streamUI`, widget composition) | Different queries spawn different widget layouts |
| 11 | Telegram bot bridge + state sync to local home server | Talk to Friday from phone, state matches desktop |
| 12 | Observability (logs, traces, latency dashboard) + bug-fix weekend | One full week of zero crashes |

**v1 exit = ambient voice-native assistant with graph memory, web/file/system/
code capability, generative dashboard, phone access. ~80% of what makes JARVIS
*feel* like JARVIS.**

---

## 13. v2 territory (months 7+ after v1 stable)

| Feature | Estimated effort |
|---------|------------------|
| Wireframe holographic avatar (TalkingHead.js + Kokoro phonemes + custom shaders + emotion engine) | 6+ weeks dedicated |
| Self-extending skills (Voyager-style, needs verifiable test harness first) | Research-grade, indefinite |
| Proactive layer (predictive, calendar/Wazuh-triggered, anticipatory) | 2+ months tuning |
| WhatsApp (Baileys) — unreliable add-on | Ongoing maintenance |
| Instagram (browser-use) — unreliable add-on | Ongoing maintenance |
| Always-on screen awareness | Define use case first |
| Watchdog sub-agent (Wazuh integration) | 2–3 weeks |
| Architect sub-agent expansion (Claude or larger local model when GPU upgrades) | Hardware-dependent |
| Multi-device full state sync (CRDT or eventual consistency) | 3–4 weeks |

---

## 14. Hard rules for v1 (do not break)

- **No autonomous skill learning.** Write skills yourself with Claude's help,
  review every line before merge.
- **No novel proactive behavior.** Scheduled proactive ("meeting in 5 min,"
  "Wazuh flagged X at 4 am") is fine and trivial. *Novel* proactive ("you
  seem stressed, want to talk?") is research-grade. Skip until v2.
- **No wireframe avatar in v1.** Particle orb only.
- **No feature that doesn't earn its keep daily.** If you don't use it for a
  week, cut it.
- **No multi-device until single-device is rock solid.** Telegram bridge is
  the only exception, and only at week 11.
- **No WhatsApp/Instagram automation in v1.** Treated as fragile v2 add-ons.
- **No continuous screen scraping.** On-demand hotkey snapshot only.

---

## 15. Rejected from various blueprints (and why)

| Rejected | Reason |
|----------|--------|
| Qwen 2.5 Coder 32B locally (original handover) | Won't fit in 16 GB VRAM alongside voice stack |
| Continuous screen awareness (original Phase 7) | Continuous OCR/vision is expensive, value vs "just ask" unclear, on-demand snapshots are the right call |
| Self-extending skills in v1 (original Phase 8) | Verification problem is unsolved research |
| Proactive layer in v1 (original Phase 9) | Signal-to-noise unsolved, needs behavioral data first |
| "Astra has phonic sharpness for Whisper" reasoning | Whisper isn't the wake-word detector; openWakeWord is; `/s/` beats `/a/` for it acoustically |
| "$0/month with Gemini free tier as council" | Rate limits cap intensive use at 1–2 hrs/day on Pro tier, quality drops on Flash-Lite |
| Claude Code as the council backend | Wrong tool — interactive CLI, not designed as backend; even Pro plan hits limits in hours of automated use; setting `ANTHROPIC_API_KEY` reverts to API pricing anyway |

---

## 16. Pre-code checklist

Answer these before writing line 1 of `router.py`:

```bash
# Hardware
nvidia-smi                        # GPU model, driver, actual VRAM
echo $XDG_SESSION_TYPE            # Wayland vs X11 → ydotool vs xdotool
cat /etc/os-release               # distro → systemd setup later
free -h                           # confirm 32 GB RAM
df -h                             # disk space for models (~30 GB total)
```

**Decisions to lock**:

- [ ] Name: Sutra / Astra / Friday → **Sutra recommended**
- [ ] Claude API monthly cap (suggest start at $30)
- [ ] Default council model: Haiku / Sonnet / Opus → **Sonnet recommended**
- [ ] Pace: 10 hrs/wk (6-month v1) or 20 hrs/wk grind (3-month v1)
- [ ] Credentials store: OS keyring (recommended) / vault / .env
- [ ] Telegram bot or PWA for phone (handover said either; Telegram easier)

---

## 17. Code order — week 1

```
friday/core/router.py         # LiteLLM tiered routing + fallback chain
friday/skills/registry.py     # @skill decorator + auto-discovery + JSON schema gen
friday/skills/web_search.py   # First real skill, hits local SearXNG container
friday/core/agent.py          # Tool-call loop: input → route → skills → answer
friday/cli.py                 # Rich-based REPL, conversation history, pretty output
```

One sitting if possible. By end of week 1: type a question → Friday routes →
calls SearXNG → answers back. That's the whole foundation.

---

## 18. Success metrics for v1 (so you know when it's done)

Not vibes-based. Measurable:

| Metric | Target |
|--------|--------|
| Voice round-trip latency (local path) | < 1500 ms p50 |
| Voice round-trip latency (cloud path) | < 2200 ms p50 |
| Memory recall accuracy on facts < 30 days old | > 90% |
| Memory recall accuracy on facts < 6 months old | > 75% |
| Action confirmation false-positive rate | < 1% (never confirms on read-only) |
| Action confirmation false-negative rate | 0% (never skips confirmation on irreversible) |
| Skills coverage | 15+ working skills |
| Uptime (Friday is responsive when you call her) | > 99% over rolling 7 days |
| Daily query volume (you actually using her) | > 30/day for 14 consecutive days |

If those numbers hold for 2 weeks straight, v1 ships. If they don't, fix
before moving to v2.

---

## 19. End state we're building toward (unchanged from handover, deferred)

> *Dark interface. Idle: a swirling cloud of cyan particles. I walk in.
> "Hey Sutra." Particles snap together — a wireframe head, glowing nodes, code
> patterns flowing through. Eyes light.*
>
> *"Morning, Harshil. You have three meetings, Wazuh flagged something at 4am
> worth looking at, and Project Alpha has a failing test. What do you want
> first?"*
>
> *"Show me Wazuh." Her head holds my gaze. The space behind her dissolves
> into a live Wazuh dashboard. She narrates the anomaly. Particles in her form
> ripple with each word.*
>
> *"Patch it." Her eyes flick — I can see her thinking. A diff appears
> mid-air. "Here's what I'd change. Approve?" I nod. "Done. Tests passing.
> Pushed."*

That's v3+. v1 gets you the *brain* underneath this scene. v2 gets you the
*face*. v3 stitches them together.

Build the brain first. The rest follows.

---

## 20. What this doc is NOT

- Not the code. The code starts after the pre-code checklist (§16).
- Not permanent. Re-read at end of month 1 and month 2 to re-scope based on
  what actually worked.
- Not a marketing document. Internal spec. Optimized for honesty and
  decision-traceability, not vibes.

---

**Status**: Ready to write code. Run the §16 commands and pick the §16
decisions. Then `router.py` first.
