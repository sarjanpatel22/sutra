# Roadmap Brainstorm — Full Plan vs 2026 SOTA

> Research pass over the comprehensive roadmap (`FRIDAY_PHASES.md` v2). For each
> major component: what's **planned**, the **2026 landscape**, and a **call**
> (keep / swap / add / watch). Companion to `MODERNIZATION_AUDIT.md` (which covers
> the *current* stack — Kokoro, Graphiti, LiteLLM all still validated; don't churn
> them). This doc covers the *future* phases and what arrived since the plan was written.
>
> Done 2026-06-04.

---

## ⭐ Headline: the plan has an MCP-shaped hole

The single biggest thing that arrived since this roadmap was written is **MCP
(Model Context Protocol)** becoming the 2026 integration substrate — and the plan
**never mentions it.** This matters in three places at once:

1. **Skills (1.1):** Sutra hand-rolls a `@skill` registry. In 2026 the same
   capabilities ship as **MCP servers** — hundreds of them, off the shelf. Sutra
   should become an **MCP host/client**: keep the local `@skill` registry for
   first-party/latency-critical skills, but gain filesystem, browser, GitHub,
   Slack, search, etc. for free by speaking MCP. This is additive, not a rewrite.
2. **Browser (1.5):** Playwright now ships an official **Playwright MCP** *and* a
   **Playwright CLI** that uses **4–100× fewer tokens** than the MCP for the same
   flows — the token-efficient default in 2026.
3. **n8n (2.1):** **Activepieces** (n8n's closest OSS rival) ships "the world's
   largest open-source MCP toolkit" (~400 MCP servers, 594 pieces usable as agent
   tools) — i.e. n8n's value increasingly *is* MCP.

**Recommendation:** add an **MCP bridge** as a first-class capability (slot it into
1.1/1.5, well before the n8n phase). It's the highest-leverage roadmap change.

---

## Per-area findings

### Sub-agents (Phase 2.6) — planned: hand-rolled Architect/Researcher/Creative/Watchdog
- **2026:** mature frameworks exist — **LangGraph** (graph state machines, lowest
  latency + token overhead, "production standard, auditable"), **Claude Agent SDK**
  (sub-agents + tool-use chains built in, safety at the model layer), **CrewAI**
  (fastest prototype, but ~3× token overhead), AG2, OpenAI Agents SDK, AWS Strands.
- **Call:** **don't fully hand-roll.** Sarjan is already on Claude → the **Claude
  Agent SDK** gives the sub-agent pattern (delegation, isolated context) for free
  and matches the "high council delegates" design. Use **LangGraph** only if you
  want explicit auditable graphs later. Keep the *user-facing* "just Sutra" framing;
  let the SDK own orchestration. Saves a big chunk of the 60 hrs budgeted for 2.6.

### Avatar / lip-sync (Phase 2.7) — planned: TalkingHead.js + Kokoro phoneme alignment
- **2026:** **NVIDIA open-sourced Audio2Face** (audio→facial animation, your RTX
  GPU is ideal for it); **MuseTalk** (Tencent) does 30+ FPS near-photoreal lip-sync;
  **HeadTTS/HeadAudio** = TalkingHead's own evolution: **Kokoro voices + viseme IDs,
  real-time lip-sync in the browser via WebGPU** — uncannily aligned with our stack
  (we already run Kokoro).
- **Call:** **HeadTTS is the natural fit** — it's TalkingHead's lineage *and*
  Kokoro-native, so phoneme alignment (the hard part the plan budgets 120 hrs for)
  largely comes for free. Keep Audio2Face/MuseTalk as the "photoreal" escalation if
  the wireframe aesthetic ever changes. Likely **cuts 2.7's effort meaningfully.**

### Browser / computer-use (Phase 1.5) — planned: Playwright + browser-use
- **2026:** **browser-use** matured into the most popular self-hosted agentic-browser
  lib (MIT, vision+Playwright, pluggable models) — the plan's pick is still right.
  But **Playwright CLI** (new 2026, 4–100× fewer tokens than MCP) is the token-smart
  primitive, and **Cua / Agent S / Stagehand** are notable computer-use options.
- **Call:** **keep browser-use** for autonomous web tasks; **add Playwright CLI/MCP**
  for deterministic, token-cheap flows (tests, scripted navigation). Defer full
  OS-level computer-use (Cua/Agent S) to v3 vision/desktop work.

### Wake word (Phase 1.8) — planned: openWakeWord
- **2026:** **openWakeWord** is still the OSS standard (and is literally trained on
  Piper TTS — ties to our deferred Piper backup). But **LiveKit wakeword** reportedly
  **beats it on every metric** with **single-command custom training**, and
  **microWakeWord** targets ESP32/low-power.
- **Call:** **try LiveKit wakeword first** (better accuracy + trivial training),
  openWakeWord as fallback. Minor delta, but free accuracy.

### Generative UI (Phase 1.10) — planned: Vercel AI SDK streamUI
- **2026:** **streamUI** is still the path of least resistance for Next.js (which the
  plan uses). New: **A2UI** (Google) — a runtime-independent JSONL UI protocol built
  for **cross-platform (web/mobile/desktop) + trust-boundary security**; **AG-UI**,
  **CopilotKit**, **assistant-ui**, **Thesys C1/Crayon** (fastest demo).
- **Call:** **keep streamUI for the v1 desktop dashboard** (1.7/1.10). **Watch A2UI**
  for the multi-device phase (1.11/3.3) — its cross-platform + security-isolation
  story fits "phone + desktop + home server" far better than a Next.js-only toolkit.

### Workflow automation (Phase 2.1) — planned: n8n
- **2026:** **Activepieces** = AI-native n8n alt (cleaner self-host, ~400 MCP servers,
  pieces usable as agent tools, free self-hosted no limits); **Windmill** = code-first
  (TS/Python/Go/Bash, audit logs); **Kestra** = orchestration-heavy.
- **Call:** **re-evaluate n8n vs Activepieces at 2.1.** If the MCP bridge lands (see
  headline), **Activepieces** is the more natural fit (MCP-first). Windmill if you'd
  rather write scripts than drag nodes (you're a SWE — plausible).

### TTS backup (Phase 1.2 — Piper, deferred today)
- **2026:** Piper is still the lightweight CPU pick (and underpins openWakeWord/HA).
  We already evaluated **Chatterbox-Turbo** → deferred (latency/PyTorch). For a *fast
  fallback if Kokoro errors*, **Piper stays the right call** (tiny, CPU, no GPU
  contention) — just not yet wired.
- **Call:** wire **Piper** as the Kokoro fallback when you do the avatar/voice polish;
  keep Chatterbox parked for an expressive non-realtime path.

---

## Net deltas to the roadmap

| Change | Where | Why |
|--------|-------|-----|
| **Add MCP host/client bridge** | new, ~1.1/1.5 | the 2026 integration substrate; hundreds of skills for free |
| **Lean on Claude Agent SDK for sub-agents** | 2.6 | don't hand-roll orchestration; saves ~weeks |
| **HeadTTS for the avatar** | 2.7 | TalkingHead lineage + Kokoro-native viseme lip-sync; saves the hard alignment work |
| **Playwright CLI alongside browser-use** | 1.5 | 4–100× token savings on scripted flows |
| **LiveKit wakeword over openWakeWord** | 1.8 | better accuracy, one-command training |
| **Re-evaluate Activepieces vs n8n** | 2.1 | MCP-first automation if the bridge lands |
| **Watch A2UI for multi-device** | 1.11/3.3 | cross-platform + security beats Next.js-only streamUI |

**Unchanged (already validated, don't touch):** Kokoro TTS, Graphiti+FalkorDB,
LiteLLM router, Silero VAD, faster-whisper, SearXNG, Tauri+Next.js shell.

**The one big idea:** thread **MCP** through the whole system early. It's the
difference between hand-building every integration and inheriting the 2026 ecosystem.

---

## Sources
- Multi-agent: [QubitTool 2026 showdown](https://qubittool.com/blog/ai-agent-framework-comparison-2026), [Firecrawl OSS agent frameworks](https://www.firecrawl.dev/blog/best-open-source-agent-frameworks)
- Avatar: [NVIDIA open-sources Audio2Face](https://developer.nvidia.com/blog/nvidia-open-sources-audio2face-animation-model/), [TalkingHead/HeadTTS](https://github.com/met4citizen/TalkingHead), [adafruit: real-time OSS 3D avatars](https://blog.adafruit.com/2026/05/13/open-source-3d-avatars-that-can-speak-and-lip-sync-in-real-time)
- Browser/computer-use: [browser-use](https://github.com/browser-use/browser-use), [Claude Code browser automation 2026](https://www.heyuan110.com/posts/ai/2026-01-28-claude-code-browser-automation/), [Computer-use agents 2026](https://www.digitalapplied.com/blog/computer-use-agents-2026-claude-openai-gemini-matrix)
- Wake word: [openWakeWord](https://github.com/dscripka/openWakeWord), [LiveKit wakeword](https://livekit.com/blog/livekit-wakeword)
- Generative UI: [A2UI vs AG-UI vs Vercel](https://qubittool.com/blog/a2ui-vs-ag-ui-vercel-agent-ui-comparison), [Generative UI frameworks 2026](https://medium.com/@akshaychame2/the-complete-guide-to-generative-ui-frameworks-in-2026-fde71c4fa8cc)
- Automation: [Best n8n alternatives 2026 (Vellum)](https://www.vellum.ai/blog/best-n8n-alternatives), [OSS n8n alternatives](https://flowlyn.com/blog/open-source-n8n-alternatives)
