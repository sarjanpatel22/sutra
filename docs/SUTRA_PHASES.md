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

## 2026-SOTA Course Corrections

> Folded in 2026-06-05 from `ROADMAP_BRAINSTORM_2026.md` (research pass over this
> plan vs the 2026 landscape). These are the deltas only — everything not listed
> here is unchanged. **Validated, do not churn:** Kokoro TTS, Graphiti+FalkorDB,
> LiteLLM router, Silero VAD, faster-whisper, SearXNG, Tauri+Next.js.

| Δ | Phase | Change | Why | Effort |
|---|-------|--------|-----|--------|
| **MCP bridge** | new **1.4b** | Sutra becomes an MCP host/client; keep local `@skill` for first-party/latency-critical, inherit the ecosystem for the rest | The 2026 integration substrate — hundreds of servers off the shelf; additive, not a rewrite | +8–12h (spike done 06-05) |
| Playwright CLI | 1.5 | Add alongside browser-use for scripted/token-cheap flows | 4–100× fewer tokens than MCP for deterministic navigation | neutral |
| LiveKit wakeword | 1.8 | Try before openWakeWord | Better accuracy + one-command custom training | neutral |
| streamUI → watch A2UI | 1.10 / 1.11 / 3.3 | Keep streamUI for v1 desktop; watch A2UI for multi-device | Cross-platform + trust-boundary security beats Next.js-only when phone+desktop+server arrive | defer |
| Activepieces vs n8n | 2.1 | Re-evaluate at phase start | If the MCP bridge lands, Activepieces (MCP-first, ~400 servers) is the more natural fit | re-eval |
| Claude Agent SDK | 2.6→pulled early | Don't hand-roll sub-agent orchestration; lean on the SDK pattern | Delegation + isolated context for free; you're already on Claude | −weeks vs 60h budget |
| HeadTTS avatar | 2.7 | Swap TalkingHead.js base → HeadTTS | TalkingHead lineage **and** Kokoro-native visemes; the hard phoneme-alignment work comes mostly free | −much of 60h |

**The one big idea:** thread **MCP** through the system early — the difference
between hand-building every integration and inheriting the 2026 ecosystem.

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
| `calc` skill | ✅ `friday/skills/calc.py` (box back-filled 06-12) |
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
| FalkorDB running (Docker) | ✅ `sutra-falkordb` container |
| Qdrant running (Docker) | ➖ dropped — design landed on Graphiti+FalkorDB only; embeddings live in the graph, no separate vector store |
| Redis running (Docker) | ✅ served by FalkorDB itself (Redis-protocol server) — no second container needed |
| Graphiti wired to FalkorDB | ✅ `friday/memory/store.py`, group_id `sarjan` |
| nomic-embed-text for embeddings | ✅ via Ollama |
| Entity extraction tuned (person, project, date) | ✅ qwen2.5:14b extraction; verified by seeded-entity recall evals |
| Memory write on every conversation turn | ✅ chat/voice persist + ambient daemon capture |
| Memory read injected into system prompt context | ✅ recall → "Relevant long-term memory" block in every system prompt |
| Cross-session recall tested (mention a person, recall next session) | ✅ measured: `scripts/eval_recall.py` hit@1 60 / hit@3 85 / hit@5 100 (06-11) |
| Memory skill: `recall`, `forget` | ✅ both registered; `forget` gated + canonical in the confirmation audit |

**Exit criteria:** Mention a person/project → recall them next session with > 90% accuracy on facts < 30 days old. **✅ Met** — top-5 injected memory reaches 100% of probed facts (hit@5 100%, 20-probe eval, 06-11). Boxes back-filled 06-12; the work itself landed Weeks 3–5.

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
| Confirmation flow tested on irreversible actions | ✅ audited + locked 06-11 — enforcement correct (declines unless approved) + classification invariant (`scripts/audit_confirmation.py`, CI guard); 0 false-negatives across skills, MCP, and delegation |
| Memory recall quality assessed | ✅ `scripts/eval_recall.py` — hit@1 60% / hit@3 85% / hit@5 100% (06-11, after narration-edge purge + atomic reseed; was 38/75/88 on 06-06) |
| Budget tracking validated against real API bills | ⬜ |
| Text + voice latency/cost instrumented (`sutra stats` p50/p95 + spend) | ✅ 06-06 |

**Exit criteria:** Use Sutra for ≥ 30 queries/day for 5 consecutive days without wanting to kill it.

**Gate 2:** Are you actually using it daily? If no — stop, diagnose, fix before Phase 1.5.
Instrumentation is now in place to judge this with numbers, not feel: every text turn
logs to `~/.local/share/friday/turn_latency.jsonl` (voice already logs `voice_latency.jsonl`);
`sutra stats` prints p50/p95 per tier/stage + rolling spend. Run the 5-day shakedown, read
`sutra stats`, fix the top-3 friction points.

---

### Phase 1.4b — MCP Bridge 🆕
**Inserted 2026-06-05 | 8–12 hrs | spike landed 06-05**

> The highest-leverage roadmap change (see Course Corrections). Sutra becomes an
> MCP host: it speaks Model Context Protocol to off-the-shelf servers (filesystem,
> git, browser, search, …) and surfaces their tools through the *existing* skill
> registry, so the agent tool-loop needs zero changes. Local `@skill` stays for
> first-party / latency-critical / OS work.

| Work item | Done? |
|-----------|-------|
| `mcp` client dependency (stdio transport) | ✅ installed |
| `MCPBridge` — connect to configured servers, list tools | ✅ spike |
| Register MCP tools into the `@skill` registry (namespaced `mcp__server__tool`) | ✅ spike |
| Claude-Desktop-compatible `mcp_servers.json` config | ✅ spike |
| Confirmation gating for MCP tools (untrusted by default → `auto_confirm=False`) | ✅ spike |
| Graceful degradation when a server is down / absent | ✅ spike |
| Per-server concurrency lock (parallel tool-calls are serialized) | ✅ |
| Wire bridge startup into chat + voice lifecycle (tools auto-join registry) | ✅ |
| MCP health check in `sutra status` / watchdog | ✅ |
| First *off-the-shelf* server (filesystem/git via npx/uvx) on hardware | ✅ filesystem (npx) — 14 tools, 06-06 |
| One-shot path (`sutra ask`) joins MCP tools too (parity with chat/voice) | ✅ |

**Exit criteria:** `sutra` can call a tool from an off-the-shelf MCP server, gated
by the same confirmation flow as first-party skills. **✅ Met 2026-06-06** —
`@modelcontextprotocol/server-filesystem` over npx surfaces 14 `mcp__filesystem__*`
tools (reads `auto`, writes `confirm`); `sutra ask "…list the docs dir…"` invoked a
read live and returned correct contents.

**Enable it:** copy `mcp_servers.example.json` → `mcp_servers.json` (gitignored,
machine-local), drop `"disabled": true`, then `sutra mcp` to connect + list tools.

---

### Phase 1.5 — Web + System Actions
> **2026 update:** keep **browser-use** for autonomous web tasks; **add the
> Playwright CLI** for deterministic/scripted flows (4–100× fewer tokens than MCP).
**Weeks 9–10 | 15–20 hrs**

| Work item | Done? |
|-----------|-------|
| Playwright installed + `browse_url` skill | ✅ 06-11 — Playwright 1.60 + chromium; read-only `browse_url` (JS-rendered pages, content-container extraction, http(s)-only, auto-runs) |
| browser-use wired for autonomous web actions | ✅ 06-11, **upgraded 06-12** — `web_task` = browser-use *pattern* over Playwright + free router (NOT the heavy pkg/langchain); observe→LLM action→execute loop, **gated**, $0. 06-12: now drives **Sutra's own persistent, visible browser** (`browser_session.py`, on-disk profile) instead of a hidden headless tab — log in once by hand, it reuses the session; added scroll + login-wall handling + main-content observation. Live-verified headed on the GPU: autonomous type→scroll→click→navigate→answer (Wikipedia search→article→correct year), $0 |
| Open Interpreter sandboxed with confirmation gate | ✅ 06-11 — gated `run_python` (isolated `python -I` subprocess + timeout), the pattern not the heavy pkg; the agent's LLM writes the code |
| xdotool skills (X11 confirmed) | ✅ 06-11 — `active_window` (read) + **gated** type_text/press_key/mouse_click (auto_confirm=False) |
| `web_extract` skill (scrape structured data) | ✅ 06-11 — CSS-selector matches (text/attr) or headings+links overview, read-only |
| `fill_form` skill (Playwright) | ✅ 06-11 — fills CSS-selector fields + optional submit; **gated** (auto_confirm=False), verified live (httpbin POST round-trip) |
| Irreversible web actions always confirmed | ✅ 06-11 — `fill_form` is auto_confirm=False → routes through the same confirm gate as write_file/run_command (sub-agents decline it) |
| Desktop **vision** take-over (beyond the browser) | ✅ 06-14 — `guide_me`/`sutra guide` looks at the real screen and says which control to click (read-only); `act_on_screen`+`type_into`/`sutra do` OCR-locate a text-labelled control (tesseract, real coords) and click/type via xdotool — **gated** (auto_confirm=False, `-y` skips), hands-free only under `SUTRA_AUTONOMOUS`. `vision/screen.py` (capture + average-hash) + `vision/locate.py` (OCR). Honest limit: text-labelled controls only (icon-only buttons aren't found) |

**Exit criteria:** Sutra can browse and act on a site you specify. **✅ Met 2026-06-11** —
`browse_url`/`web_extract` (read), `fill_form` + xdotool input (gated act), `web_task`
(autonomous browse-use pattern over the free router), `run_python` (gated code exec).
All irreversible actions route through the confirm gate; deterministic + autonomous, $0.

> **JARVIS upgrade — 2026-06-14 (verified):** the always-on, watch-and-help layer landed.
> **Brain** — `core/autoroute.py` `chat_tier()` sends substantive chat to the free Groq
> 70B and keeps trivial greetings on local gemma3:4b (used by `voice/pipeline.py` on the
> non-action paths). **`sutra watch`** (`friday/watch/`) — ambient loop that screenshots
> only when the screen changes, asks a free vision model for ONE genuinely-useful
> suggestion (silent otherwise) and surfaces it as a desktop notification; OFF by default,
> `--once` flag, tuned via `WATCH_*` env, screenshots go to Groq only on change.
> **`guide_me`** (read-only) + **`act_on_screen`/`type_into`** (the OCR+xdotool take-over,
> gated / autonomous-only for voice) give Sutra desktop control beyond the browser.

---

### Phase 1.6 — IDE Bridge
**Weeks 11–12 | 25–30 hrs**

| Work item | Done? |
|-----------|-------|
| Continue.dev or Cline pattern wired | ✅ the pattern, not the plugin: see repo → targeted edit → run tests → review → commit, all through the existing agent loop + confirmation gates |
| `edit_file` skill (targeted, not whole-file replace) | ✅ exact-snippet replace, unique-match required; gated (06-12) |
| `run_tests` skill (pytest, specific file or suite) | ✅ pytest in repo, summary + output tail; gated — executes repo code (06-12) |
| `git_status / git_diff / git_commit` skills | ✅ reads auto, commit gated; `commit` added to audit verbs (06-12) |
| Code review skill (reads diff, returns comments) | ✅ `code_review` — working/staged/ref diffs → numbered findings; live-verified on a real diff via local qwen (06-12) |
| Qwen 3.5 9B on-demand load for code tasks | ✅ as qwen2.5:14b (already on disk): new `Tier.CODE` routes to it, Ollama loads it on first call; free cloud fallback |
| Qwen unloads between sessions (LiteLLM managed) | ✅ Ollama-managed (not LiteLLM): keep_alive idle eviction frees the VRAM between code sessions |

**Exit criteria:** Sutra can run terminal commands and edit code you point it at. **✅ Met 2026-06-12** — `run_command` (1.1) + `edit_file`/`run_tests`/`git_*`/`code_review`, every mutation behind the confirm gate.

---

### Phase 1.7 — Dashboard MVP
**Weeks 13–15 | 35–40 hrs**

| Work item | Done? |
|-----------|-------|
| Tauri scaffold + Next.js 14 inside | ➖ deliberately deferred — MVP shipped on the proven stdlib-serve + chromium `--app` path (zero toolchain, $0); revisit Tauri when packaging/distribution matters |
| Tailwind + shadcn/ui + assistant-ui wired | ➖ same call: single-file zero-dep `dashboard.html` in the locked gold-on-obsidian orb language |
| Widget: memory graph (FalkorDB vis) | ➖ exists standalone (`sutra graph`, full 3D viewer); embedding it into the dashboard = next slice |
| Widget: task/skill state | ✅ live action feed (tool_start/tool_end via agent `observe` hook) + gate badges + skill posture counts (06-12) |
| Widget: file/code preview | ⬜ next slice |
| Widget: web cards (search results) | ⬜ next slice |
| Widget: settings panel | ➖ partial: live model-tier selector in the composer; full panel later |
| Vercel AI SDK `streamUI` wired for generative layout | ➖ deferred with the React arc (generative layout is Phase 1.10) |
| WebSocket connection to Friday backend | ✅ as SSE + POST (stdlib has no WS server; same live-stream capability, simpler protocol): `/api/events` replay+heartbeat stream, `/api/chat`, `/api/confirm` (06-12) |

**Exit criteria:** GUI launches, connects to live backend, shows real state. **✅ Met 2026-06-12** — `sutra dashboard` opens a borderless window: glass panels over the live orb, real agent turns (free tier, tool calls visible as they fire), live p50/p95 + spend telemetry, and **confirmation cards** — the gate on screen: Sutra asks permission before any irreversible skill, click to approve/decline, silence declines after 120s. Bridge + server fully tested over real HTTP (no LLM in tests).

---

### Phase 1.8 — Wake Word
**Week 16 | 15 hrs**
> **2026 update:** try **LiveKit wakeword** first (beats openWakeWord on accuracy,
> single-command custom training); openWakeWord as the fallback.

| Work item | Done? |
|-----------|-------|
| openWakeWord installed | ✅ on the existing onnxruntime (no torch); pretrained models ship bundled |
| Custom "Hey Sutra" model trained | ⬜ — pretrained "hey_jarvis" is the working default; a custom model needs a training pipeline (follow-on) |
| Always-listening mode (low CPU, GPU-idle path) | ✅ `sutra voice --mode wake` — `friday/voice/wakeword.py` + `pipeline.wake_listen_loop`, opt-in, dormant→wake→VAD turn; gate unit-tested (`tests/test_wakeword.py`). Live-mic accuracy is Sarjan's to validate (far-field SNR caveat) |
| Barge-in: Sutra stops speaking when you start | ✅ landed early with the voice loop (VAD barge-in + `scripts/barge_in_smoke.py`) |
| Speaker verification (basic — reject strangers) | ⬜ — deferred: needs a speaker-embedding model (don't pull torch); openWakeWord's custom-verifier is a candidate path |

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
| **2.6** | Wireframe Avatar — **HeadTTS** base (TalkingHead lineage, Kokoro-native visemes → alignment mostly free), lip sync, emotion engine, idle dissolve | ~30 (was 60+) |
| **2.7** | Watchdog + Wazuh Symmetry — background monitoring, alert routing, anomaly narration | 25 |

> **2026 updates to v2:** **2.1** — re-evaluate **Activepieces** (MCP-first) vs n8n
> at phase start. **Sub-agents** (when added) — lean on the **Claude Agent SDK**
> delegation pattern rather than hand-rolling orchestration. **2.6** — **HeadTTS**
> over raw TalkingHead.js (Kokoro-native lip-sync cuts the alignment work).

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
