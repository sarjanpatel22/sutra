# Friday OS — Project Handover

> Paste this into a new Claude chat to pick up exactly where we left off.
> Everything you need to know about the project, the architecture, the
> decisions made, and what's next.

---

## TL;DR for the next Claude

I'm Harshil. I'm building a personal AI assistant called **Friday** — inspired
by Tony Stark's JARVIS / Friday. Not a chatbot. A full ambient presence:
voice-native, holographic avatar, graph memory, agentic, controls my computer,
proactive. We've locked the vision and stack and started scaffolding the repo.
Continue building with me. Read this whole doc first, then ask what to tackle
next.

---

## My setup

- **OS**: Linux
- **RAM**: 32GB
- **VRAM**: 16GB (GPU type to confirm — likely NVIDIA)
- **Location**: Toronto
- I have a Wazuh dashboard running on my LAN (192.168.23.129) — relevant
  because Friday will eventually monitor / interact with it

---

## The vision (locked)

Friday is **not an assistant you open** — she's an **ambient presence**:

- **Voice-native** — you talk, she talks back, real-time, interruptible
- **Holographic avatar** — Path A + D: idle = swirling cyan particles, when
  addressed they coalesce into a wireframe head with glowing nodes and
  audio-reactive lip sync. Dissolves back when conversation ends. JARVIS-grade
  aesthetic, never goes near uncanny valley.
- **Generative UI** — the dashboard reshapes per query. Ask "what's happening
  in the world" → news widgets, video grid, map, trending topics auto-render.
  Ask "brief me on Project Alpha" → memory graph + recent files + timeline.
  Claude picks which widgets to compose.
- **Graph memory** — Graphiti-style. Entities, relationships, bi-temporal.
  Visual node-graph browser in the dashboard. She remembers people, projects,
  habits, and how they all connect.
- **Truly capable** — sends WhatsApp messages, searches Instagram, books
  flights, controls smart home, runs my computer, pair-programs with me
- **Self-extending** — Voyager-style autonomous skill learning from GitHub
- **Proactive** — knows my patterns, anticipates needs, speaks up when it
  matters
- **Multi-instance** — desktop (main), phone (Telegram bot or PWA), home
  server (always-on), state syncs in real-time

The line is: "intention → outcome, with Friday as the invisible layer that
makes it happen."

---

## Stack (locked, all free / open-source)

| Layer       | Tools                                                              |
| ----------- | ------------------------------------------------------------------ |
| **Brain**   | Claude (high council) + Groq + Gemini free tiers + Gemma 4 (local) |
| **Routing** | LiteLLM — multi-provider, swappable, with fallback chain           |
| **Voice STT** | faster-whisper Large-v3 (local, ~3GB VRAM)                       |
| **Voice TTS** | Kokoro (best free quality) with Piper as backup                  |
| **Wake word** | openWakeWord (custom-train "Hey Friday")                         |
| **Voice pipeline** | Pipecat (MIT-licensed, real-time framework)                 |
| **Graph memory** | Graphiti + FalkorDB                                           |
| **Vector memory** | Qdrant                                                       |
| **Structured** | SQLite (personal), Postgres if it grows                         |
| **Web automation** | Playwright + browser-use                                    |
| **System control** | Open Interpreter                                            |
| **Desktop input** | ydotool (Wayland) / xdotool (X11)                            |
| **Workflows** | n8n self-hosted (integrations, scheduling, webhooks)             |
| **Search** | SearXNG self-hosted (no rate limits, private)                       |
| **Backend** | Python 3.11+ / FastAPI / asyncio / Redis                           |
| **Frontend** | Next.js 14 + Tailwind + shadcn/ui + assistant-ui                   |
| **Streaming UI** | Vercel AI SDK (`streamUI` for generative UI)                  |
| **Avatar** | Three.js + TalkingHead.js + custom shaders + Kokoro phoneme sync    |
| **Animations** | Framer Motion                                                   |
| **Desktop wrap** | Tauri (Rust-based, native, small)                             |
| **Maps** | MapLibre (fully free)                                                |
| **Coding skills** | Steal from Continue.dev / Aider / Cline                       |
| **WhatsApp** | whatsapp-web.js or Baileys (unofficial — burner account)          |

**Monthly cost target**: ~$0 software + ~$5–20 optional Claude API for the
hardest reasoning. Groq + Gemini free tiers + local Gemma 4 handle the rest.

---

## Architecture — the "high council" pattern

```
User (voice / text / camera / screen)
   ↓
Router (tiny Gemma 4 E4B locally — intent classification)
   ↓
   ├── Simple ("what time")            → handled directly
   ├── Medium                          → Groq Llama 3.3 70B (free, fast)
   └── Complex / multi-step / reason   → Claude (the council)
              ↓
         Claude plans, delegates sub-tasks to Gemma/Groq workers,
         synthesizes, returns:
            - voice_response   (Kokoro speaks it, avatar lip-syncs)
            - layout_spec      (which widgets to render in the dashboard)
            - tool_calls       (skills to execute in parallel)
   ↓
Tools run → stream results back → widgets mount and animate in
```

For specialized domains, Claude delegates to sub-agents:
- **Architect** (Qwen 2.5 Coder 32B local or Claude) — coding tasks
- **Researcher** (long-context model) — deep dives
- **Creative** — brainstorming, writing
- **Watchdog** (small fast) — continuous monitoring

User only ever experiences "Friday."

---

## Phase roadmap

- **Phase 1 — Brain stem** (THIS WEEKEND): multi-model router, skill registry,
  one skill (web search via SearXNG), CLI to talk to her.
- **Phase 2 — Voice**: Whisper + Kokoro + Pipecat pipeline. Push-to-talk first,
  wake word later.
- **Phase 3 — More skills**: file ops, email, calendar, WhatsApp (Baileys),
  Instagram (browser-use), basic computer control.
- **Phase 4 — Memory**: Graphiti + FalkorDB. Migrate from session-only memory
  to persistent graph.
- **Phase 5 — Frontend dashboard**: Next.js + shadcn + assistant-ui + Vercel AI
  SDK for generative UI. Widget library: chat, video grid, map, charts, memory
  graph view, code editor, terminal, browser preview, recipe card, etc.
- **Phase 6 — The holographic avatar**: Three.js scene, TalkingHead.js base,
  custom shaders for the wireframe-particle aesthetic, Kokoro-driven lip sync,
  emotion engine from voice tone, idle dissolve + wake-word coalesce.
- **Phase 7 — Computer use**: always-on screen awareness, IDE bridge (steal
  from Continue.dev) for voice-driven pair programming, full Playwright skills.
- **Phase 8 — Self-extending skills**: Voyager-style. Friday searches GitHub,
  Claude reads READMEs + code, writes skill module, tests in sandboxed Docker,
  registers if passes.
- **Phase 9 — Proactive + multi-instance**: n8n schedules, predictive layer
  (knows my patterns), Telegram bot for phone, state sync across devices.

**Realistic timeline**: 4–6 months part-time for the full vision.
Foundation usable after week 1.

---

## What's already built (Phase 1 scaffolding started)

In `/home/claude/friday-os/`:

```
friday-os/
├── README.md              ✓ done — vision, setup, structure
├── docker-compose.yml     ✓ done — Qdrant, FalkorDB, n8n, Redis, SearXNG
├── .env.example           ✓ done — all API keys + routing config
├── requirements.txt       ✓ done — Phase 1 Python deps
├── setup.sh               ✓ done — bootstrap script
├── friday/
│   ├── core/              ⨯ empty — needs agent.py, router.py, events.py
│   ├── skills/            ⨯ empty — needs registry.py + web_search.py
│   ├── memory/            ⨯ empty — stub for Phase 4
│   ├── api/               ⨯ empty — FastAPI server for Phase 5+
│   └── cli.py             ⨯ empty — the REPL entry point
└── frontend/              ⨯ empty — Phase 5
```

**Where we stopped**: scaffolding done, Python modules not yet written. Next
Claude should pick up by writing the Python code in `friday/`.

---

## What to do next (start here in the new chat)

In rough order:

1. **Write the LiteLLM router** (`friday/core/router.py`) — picks model by tier
   (fast / medium / smart), handles fallback chain from `.env`.
2. **Write the skill registry** (`friday/skills/registry.py`) — `@skill`
   decorator, auto-discovery, JSON schema generation for tool calling.
3. **Write the first skill** (`friday/skills/web_search.py`) — hits SearXNG
   container, returns top results.
4. **Write the agent loop** (`friday/core/agent.py`) — takes user input, asks
   router to classify intent, picks model, gives it the skill list, runs the
   tool-call loop until done, returns final answer.
5. **Write the CLI** (`friday/cli.py`) — Rich-based pretty REPL with
   conversation history. Entry: `python -m friday.cli`.

After Phase 1 works (I can type and Friday responds + uses the web search
skill), we move to Phase 2 (voice).

---

## Important design decisions already made (don't re-litigate)

- **Tauri over Electron** for desktop wrap
- **FalkorDB over Neo4j** for graph (lighter, faster)
- **Kokoro over ElevenLabs** for TTS (free, good enough quality)
- **SearXNG over Google API** (no rate limits, private)
- **n8n is the integrations hub, NOT the main brain**. Core agent loop stays in
  Python. n8n handles scheduled tasks, integration glue, proactive triggers.
- **Avatar path A + D** (holographic wireframe + idle particles), NOT
  photorealistic (uncanny valley) and NOT vtuber-style
- **No chat box in the GUI** — voice + generative UI. Chat is fallback only.
- **Local first for what works locally** (STT, TTS, embeddings, routing,
  vision), cloud for the heavy reasoning (Claude)
- **The skill registry pattern is the heart** — every capability is a Python
  function with a decorator, auto-exposed to the LLM via tool calling
- **Sub-15-word user-facing responses by default in voice mode** (long
  responses feel robotic spoken)

---

## How I work / what I want from the next Claude

- I think out loud and add stuff as I go — that's fine, don't get frustrated,
  just keep folding new ideas into the architecture
- I'm hands-on capable but I want you to do the heavy lifting on code so I can
  move fast and work on other stuff in parallel
- Push back when I'm wrong about something technical
- Be direct, no hand-holding, no over-explaining
- When you write code, write it production-grade — type hints, docstrings,
  async where it matters, sane error handling
- Don't write 500-line files when 80 lines does the job
- Show me code first, explanations after — I read code faster than prose

---

## Open questions to resolve early in the new chat

- What GPU is in my machine (NVIDIA model, driver version)? — affects inference
  runtime choice
- Wayland or X11? — affects automation tools later
- Which distro? — affects systemd setup later
- Do I want Phase 1 finished before adding voice, or parallel build?

---

## Things to remember about me

- I've already got Wazuh running on my LAN — Friday will integrate with it later
- I prefer aggressive timeline targets — say what's actually achievable but
  don't pad estimates
- I want this to be genuinely *mine* — the personality, the workflow, the
  feel. Don't let starter kits make 90% of the product decisions.

---

## End state we're building toward

A dark interface. Idle: a swirling cloud of cyan particles. I walk in.
"Hey Friday." Particles snap together — a wireframe head, glowing nodes, code
patterns flowing through. Eyes light.

"Morning, Harshil. You have three meetings, Wazuh flagged something at 4am
worth looking at, and Project Alpha has a failing test. What do you want
first?"

"Show me Wazuh." Her head holds my gaze. The space behind her dissolves into a
live Wazuh dashboard. She narrates the anomaly. Particles in her form ripple
with each word.

"Patch it." Her eyes flick — I can *see* her thinking. A diff appears mid-air.
"Here's what I'd change. Approve?" I nod. "Done. Tests passing. Pushed."

That's the product.

Let's build it.
