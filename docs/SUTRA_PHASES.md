# Sutra — Consolidated Phases

> Single source of truth for all phases, exit criteria, and validation gates.
> If this doc and the spec disagree, the spec wins on architecture; this doc wins on sequencing.

---

## Total honest math

| Version | Hours | Calendar at 10 hrs/wk |
|---------|-------|-----------------------|
| v1 | ~245 | 6–7 months |
| v1 + v2 | ~580 | 14–17 months |
| v1 + v2 + v3 | ~840 | 22–25 months |

Two years of disciplined side-project work. Plan for milestones, not weeks.

---

## The 5 Validation Gates

Stop and honestly assess "should I keep going" at each of these.

| Gate | When | Question |
|------|------|----------|
| **Gate 1** | End of Phase 1.1 (Week 2) | Does the brain stem actually work? |
| **Gate 2** | End of Phase 1.4 (Week 8) | Are you using Sutra daily? If not, the whole project is in trouble. |
| **Gate 3** | End of v1 (Week 24) | Did you hit the exit criteria? If not, do **not** start v2. |
| **Gate 4** | End of Phase 2.5 (Month 14) | Is Sutra a real colleague, not just a tool? |
| **Gate 5** | Before v3 | Is v2 actually used, or did you over-build? |

---

## v1 — The Foundation

**Target:** ~6–7 months at 10 hrs/wk (~245 hrs)

**v1 exit criteria (all must hold for 2 weeks):**
- Voice round-trip latency < 1500 ms p50 (local path)
- Memory recall accuracy > 90% on facts < 30 days old
- Action confirmation false-negative rate = 0% (never skips confirmation on irreversible)
- 15+ working skills
- Uptime > 99% over rolling 7 days
- Daily query volume > 30/day for 14 consecutive days

---

### Phase 1.1 — Brain Stem
**Weeks 1–2 | 20–25 hrs**

| Work item | Done? |
|-----------|-------|
| LiteLLM tiered router + fallback chain | ✅ |
| Budget tracker with 85% alerts, persistent state | ✅ |
| `@skill` decorator + auto-discovery + JSON schema gen | ✅ |
| `web_search` skill (SearXNG) | ✅ |
| `file_ops` skill (read/write/list) | ✅ |
| `system_info` + `run_command` skill | ✅ |
| `calc` skill | ⬜ |
| Tool-call agent loop (parallel skill execution) | ✅ |
| Rich REPL (`sutra chat / ask / skills / status / budget`) | ✅ |
| Health + budget watchdog (`sutra monitor`) | ✅ |
| NVIDIA drivers installed, nvidia-smi working | ✅ |

**Exit criteria:** Type a question → Sutra routes → calls a skill → answers back. ✅

**Gate 1:** Brain stem works. Proceed to voice.

---

### Phase 1.2 — Voice Loop
**Weeks 3–4 | 20–25 hrs**

| Work item | Done? |
|-----------|-------|
| faster-whisper Large-v3 INT8 on CUDA | ✅ ~420ms warm |
| Kokoro TTS ONNX | ✅ GPU (CUDAExecutionProvider) — warm ~74ms / 2.7s audio, RTF 0.028 |
| Push-to-talk mode (spacebar hold) | ✅ `sutra voice` |
| VAD continuous mode | ✅ `sutra voice --mode vad` |
| Streaming TTS (speak before LLM finishes) | ✅ sentence-boundary streaming |
| VAD tuned to ~350 ms silence threshold | ✅ webrtcvad aggressiveness=2 |
| System prompt enforces sub-15-word spoken responses | ✅ VOICE_SYSTEM_PROMPT |
| Gemma 3:4b via Ollama for local voice path | ✅ 3.3GB, ~520ms warm |
| Voice latency measured and logged | ✅ ~/.local/share/friday/voice_latency.jsonl |

**Exit criteria:** Speak a question → get spoken answer. Local path p50 < 1500 ms.
**Status:** ✅ Functional — Kokoro GPU TTS now working (onnxruntime-gpu 1.20.1,
CUDAExecutionProvider), warm synth ~74ms. Estimated local round-trip ~1.0s
(STT ~420ms + Gemma ~520ms + TTS ~74ms), under the 1500ms target. Pending: live
mic round-trip measurement logged to voice_latency.jsonl.

**Dependencies:** Ollama 0.30.0 installed ✅, gemma3:4b pulled ✅, PipeWire audio confirmed ✅

---

### Phase 1.3 — Memory
**Weeks 5–6 | 25–30 hrs**

| Work item | Done? |
|-----------|-------|
| FalkorDB running (Docker) | ⬜ |
| Qdrant running (Docker) | ⬜ |
| Redis running (Docker) | ⬜ |
| Graphiti wired to FalkorDB | ⬜ |
| nomic-embed-text for embeddings | ⬜ |
| Entity extraction tuned (person, project, date) | ⬜ |
| Memory write on every conversation turn | ⬜ |
| Memory read injected into system prompt context | ⬜ |
| Cross-session recall tested (mention a person, recall next session) | ⬜ |
| Memory skill: `recall`, `forget` | ⬜ |

**Exit criteria:** Mention a person/project → recall them next session with > 90% accuracy on facts < 30 days old.

**Dependencies:** Docker installed, 8 GB+ free disk for containers + embeddings.

---

### Phase 1.4 — Daily-Driver Shakedown ⚠️
**Weeks 7–8 | 15–20 hrs**

> This is the first big validation gate. The goal is not to add features — it is to find everything that is broken or slow when you actually use Sutra for real work.

| Work item | Done? |
|-----------|-------|
| Use Sutra for real daily tasks for 5 consecutive days | ⬜ |
| Latency profiled and p50/p95 logged | ⬜ |
| Top 3 friction points identified and fixed | ⬜ |
| Confirmation flow tested on irreversible actions | ⬜ |
| Memory recall quality assessed | ⬜ |
| Budget tracking validated against real API bills | ⬜ |

**Exit criteria:** Use Sutra for ≥ 30 queries/day for 5 consecutive days without wanting to kill it.

**Gate 2:** Are you actually using it daily? If no — stop, diagnose, fix before Phase 1.5.

---

### Phase 1.5 — Web + System Actions
**Weeks 9–10 | 15–20 hrs**

| Work item | Done? |
|-----------|-------|
| Playwright installed + `browse_url` skill | ⬜ |
| browser-use wired for autonomous web actions | ⬜ |
| Open Interpreter sandboxed with confirmation gate | ⬜ |
| xdotool skills (X11 confirmed) | ⬜ |
| `web_extract` skill (scrape structured data) | ⬜ |
| `fill_form` skill (Playwright) | ⬜ |
| Irreversible web actions always confirmed | ⬜ |

**Exit criteria:** Sutra can browse and act on a site you specify.

---

### Phase 1.6 — IDE Bridge
**Weeks 11–12 | 25–30 hrs**

| Work item | Done? |
|-----------|-------|
| Continue.dev or Cline pattern wired | ⬜ |
| `edit_file` skill (targeted, not whole-file replace) | ⬜ |
| `run_tests` skill (pytest, specific file or suite) | ⬜ |
| `git_status / git_diff / git_commit` skills | ⬜ |
| Code review skill (reads diff, returns comments) | ⬜ |
| Qwen 3.5 9B on-demand load for code tasks | ⬜ |
| Qwen unloads between sessions (LiteLLM managed) | ⬜ |

**Exit criteria:** Sutra can run terminal commands and edit code you point it at.

---

### Phase 1.7 — Dashboard MVP
**Weeks 13–15 | 35–40 hrs**

| Work item | Done? |
|-----------|-------|
| Tauri scaffold + Next.js 14 inside | ⬜ |
| Tailwind + shadcn/ui + assistant-ui wired | ⬜ |
| Widget: memory graph (FalkorDB vis) | ⬜ |
| Widget: task/skill state | ⬜ |
| Widget: file/code preview | ⬜ |
| Widget: web cards (search results) | ⬜ |
| Widget: settings panel | ⬜ |
| Vercel AI SDK `streamUI` wired for generative layout | ⬜ |
| WebSocket connection to Friday backend | ⬜ |

**Exit criteria:** GUI launches, connects to live backend, shows real state.

---

### Phase 1.8 — Wake Word
**Week 16 | 15 hrs**

| Work item | Done? |
|-----------|-------|
| openWakeWord installed | ⬜ |
| Custom "Hey Sutra" model trained | ⬜ |
| Always-listening mode (low CPU, GPU-idle path) | ⬜ |
| Barge-in: Sutra stops speaking when you start | ⬜ |
| Speaker verification (basic — reject strangers) | ⬜ |

**Exit criteria:** "Hey Sutra" wakes her from idle, barge-in interrupts cleanly.

---

### Phase 1.9 — Aurora/Horizon Visual
**Weeks 17–19 | 30–35 hrs**

| Work item | Done? |
|-----------|-------|
| Three.js canvas in Tauri window | ⬜ |
| Particle system: idle HORIZON state (slow drift, dawn palette) | ⬜ |
| Particle system: active AURORA state (cyan/violet, reactive) | ⬜ |
| Audio-reactive: orb pulses with voice amplitude | ⬜ |
| Custom GLSL shaders for particle behaviour | ⬜ |
| State transitions: HORIZON → AURORA on wake, AURORA → HORIZON on idle | ⬜ |
| Framer Motion transitions for panel mounts | ⬜ |

**Exit criteria:** Orb pulses with her voice and yours. Transitions feel deliberate, not janky.

**Reference:** `docs/FRIDAY_VISUAL_DIRECTION.md` — AURORA/HORIZON spec, palette, motion language.

---

### Phase 1.10 — Generative UI
**Weeks 20–21 | 25 hrs**

| Work item | Done? |
|-----------|-------|
| Vercel AI SDK `streamUI` fully wired | ⬜ |
| Widget composition from LLM layout spec | ⬜ |
| MapLibre widget (location queries) | ⬜ |
| Code diff widget | ⬜ |
| Chart widget (data queries) | ⬜ |
| Layout persists across turns in same session | ⬜ |

**Exit criteria:** Different queries spawn meaningfully different widget layouts.

---

### Phase 1.11 — Telegram Bridge
**Weeks 22–23 | 15–20 hrs**

| Work item | Done? |
|-----------|-------|
| Telegram bot registered + python-telegram-bot wired | ⬜ |
| Text queries proxied to Friday agent | ⬜ |
| Session state synced to Redis (shared with desktop) | ⬜ |
| Photo/file pass-through to vision skill | ⬜ |
| Irreversible action confirmations work over Telegram | ⬜ |

**Exit criteria:** Talk to Sutra from phone, state matches desktop.

---

### Phase 1.12 — Observability
**Week 24 | 15 hrs**

| Work item | Done? |
|-----------|-------|
| Structured logging (JSON, every agent turn) | ⬜ |
| OpenTelemetry traces wired | ⬜ |
| Latency dashboard (p50/p95 per tier + skill) | ⬜ |
| Cost dashboard (per-model, per-day rolling) | ⬜ |
| Wazuh integration stub (v2 full integration) | ⬜ |
| One full week of zero crashes | ⬜ |

**Exit criteria:** One full week of zero crashes. All v1 success metrics holding.

**Gate 3:** Did you hit the v1 exit criteria? If not — fix before starting v2.

---

## v2 — Polish + Producer Modes

**Target:** ~9–10 months after v1 (~335 hrs)

> Do not start v2 until Gate 3 passes.

| Phase | What | Est. hrs |
|-------|------|----------|
| **2.1** | Producer Mode Framework — context state machine, mode bundles (Streaming / Bounty / Meeting / Deep Work) | 30 |
| **2.2** | Streaming Mode — OBS integration, chat reading, clip suggestions | 25 |
| **2.3** | Bounty Mode — scope checks, recon skills, report templates | 30 |
| **2.4** | Pattern-Driven Skill Proposal — Level 2 self-extension (suggests skills, you approve + review) | 35 |
| **2.5** | Personality Polish — PERSONA.md constitution, affect detection, tone matching | 20 |
| **2.6** | Wireframe Avatar — TalkingHead.js base, Kokoro phoneme alignment, lip sync, emotion engine, idle dissolve | 60+ |
| **2.7** | Watchdog + Wazuh Symmetry — background monitoring, alert routing, anomaly narration | 25 |

**Gate 4:** Is Sutra a real colleague, not just a tool?

---

## v3 — Frontier

**Target:** ~7–8 months after v2 (~260 hrs)

> Do not start v3 until Gate 5 passes.

| Phase | What | Notes |
|-------|------|-------|
| **3.1** | Validated Skill Auto-Refinement | Level 3 (SkillOpt fork, read-only discovery) |
| **3.2** | Proactive Layer | Hardest unsolved problem. Needs 6+ months of behavioral data first. |
| **3.3** | Multi-Device Full Sync | CRDT or eventual consistency, home server, mobile |
| **3.4** | Always-On Screen Awareness | Define use case before building |
| **3.5** | WhatsApp / Instagram | Treated as unreliable add-ons throughout |

---

## v4+ — Research (Indefinite)

Level 4 autonomous skill generation, full emotion engine, self-evolving system, Sutra as platform.

**Do not bolt onto Sutra.** Scope these as separate research projects.

---

## Hard rules (v1, do not break)

- No autonomous skill learning — write skills yourself, review every line
- No novel proactive behavior — scheduled alerts fine, "you seem stressed" is not
- No wireframe avatar — particle orb only
- No feature that doesn't earn its keep daily
- No multi-device until single-device is rock solid (Telegram is the only exception, Week 22)
- No continuous screen scraping — on-demand hotkey snapshot only
