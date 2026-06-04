# Sutra Modernization Audit — 2026-06-04

How every major component stacks up against 2026 state-of-the-art, the way the
voice dig found webrtcvad was obsolete. For each: what we run, what's current,
the verdict, and the recommended move. Ranked by impact at the bottom.

> Method: web research into the open frameworks and leaderboards (LiveKit/Pipecat
> blogs, OpenASR leaderboard, MTEB, LongMemEval/LoCoMo, Ollama library). Sources
> inline.

---

## Component-by-component

### 1. Voice Activity Detection — ✅ ALREADY MODERNIZED
- **Was:** `webrtcvad` (2016, frame-based) → fragmented real speech, fooled by noise.
- **Now:** **Silero VAD** (neural, noise-robust) — what LiveKit/Pipecat/TEN use. Shipped `97dbb25`.
- **Verdict:** Fixed. speech 0.92 / silence 0.004 separation.

### 2. Speech-to-Text — 🔴 BIGGEST REMAINING WIN
- **Now:** `faster-whisper distil-large-v3`, **record → then transcribe** (dead air after you stop).
- **SOTA 2026:**
  - **NVIDIA Parakeet TDT** — sub-5% WER, **3–6× faster than Whisper**, RTFx >2000. Best accuracy+speed *on our NVIDIA GPU*. (NeMo framework — heavier.)
  - **Moonshine v2** — 245M, purpose-built **streaming** ("Ergodic Streaming Encoder"), matches Whisper-large-v3 on English, tiny footprint.
- **The real gap isn't the model — it's the *architecture*.** Commercial assistants
  **stream STT while you talk**, so the transcript is done the instant you stop.
  We wait, then transcribe.
- **Recommend:** Phase 2 = streaming STT (chunked faster-whisper / RealtimeSTT now;
  evaluate Moonshine for native streaming, Parakeet for max accuracy).

### 3. Local LLM (voice handler) — 🟡 UPGRADE CANDIDATE
- **Now:** `gemma3:4b` (Ollama).
- **SOTA 2026:**
  - **Gemma 3n (E2B/E4B)** — *audio-native* (text+image+audio in), built for on-device, 140+ langs. Could eventually enable speech understanding without separate STT.
  - **Qwen3 (4B/7B)** — strongest agentic/reasoning under 8B.
  - **Llama 3.2 3B** — widest/most-reliable function-calling + framework support.
  - **Gemma 4** exists now (we tested `e4b` — Ollama build was broken: slow + empty replies; revisit after an Ollama update).
- **Recommend:** trial **Qwen3-4B** (reasoning) or **Llama-3.2-3B** (tool reliability) vs gemma3:4b on the voice loop; watch Gemma 3n for audio-native.

### 4. Text-to-Speech — ✅ GOOD CHOICE (keep)
- **Now:** `Kokoro-82M` (ONNX, GPU).
- **SOTA 2026:** Kokoro is *still* the recommended real-time/edge pick (RTF 0.03, runs anywhere). **Chatterbox-Turbo** (350M, <200ms, emotion tags `[laugh]`/`[cough]`, voice cloning, beat ElevenLabs 63% in blind tests) is the quality/expressiveness upgrade (watermarked).
- **Verdict:** Keep Kokoro for latency. Chatterbox optional if we want expressive/emotional speech.

### 5. Agent Memory — ✅ TOP-TIER CHOICE (keep)
- **Now:** **Graphiti** + FalkorDB (temporal knowledge graph).
- **SOTA 2026:** Graphiti *is* Zep's engine — Zep scores **63.8% LongMemEval vs Mem0's 49%** (temporal KG wins). **Letta/MemGPT** (~83% LoCoMo) is the main alt (OS-style core/archival/recall memory).
- **Verdict:** Our choice is validated as a leader. The recall pain was *config* bugs (wrong graph, dead `.search()`) — already fixed (`6394400`, `1f5499a`). Consider Letta's explicit memory-tier pattern later for agent self-management.

### 6. Embeddings — 🟡 EASY QUALITY WIN
- **Now:** `nomic-embed-text` (MTEB **62.4**, 274MB).
- **SOTA 2026:** **Qwen3-Embedding** (#1 multilingual MTEB **70.6**; comes in 0.6B/4B/8B). BGE-M3 strong for hybrid/multilingual.
- **Recommend:** swap to **Qwen3-Embedding-0.6B** (or 4B) — directly improves the recall *ranking* we flagged as "decent-not-perfect," small VRAM. Re-embeds the graph.

### 7. Router / framework — 🟢 FINE
- LiteLLM tiered (local→Groq→Gemini→Claude) is a sound pattern. Model names current
  (gemini-2.5-flash fixed). No change needed.

### 8. Voice orchestration — 🔵 STRATEGIC DECISION
- We **hand-roll** the mic→VAD→STT→LLM→TTS loop. Frameworks **Pipecat** / **LiveKit
  Agents** give streaming, turn-detection, and **barge-in/interruption** for free.
- **Trade-off:** control + no heavy deps (ours) vs. instant SOTA turn-taking (theirs).
  Worth a spike to compare once streaming STT lands.

---

## Priority ranking (impact on "better than the competition")

| # | Move | Impact | Effort |
|---|------|--------|--------|
| 1 | **Streaming STT** (transcribe while speaking) | 🔴 the "instant answer" feel | High |
| 2 | **Qwen3-Embedding** (recall ranking) | 🟡 sharper memory | Low |
| 3 | **Trial Qwen3-4B / Llama-3.2-3B** voice LLM | 🟡 smarter replies | Med |
| 4 | **Barge-in/interruption** (or adopt Pipecat/LiveKit) | 🟡 natural turn-taking | High |
| 5 | Chatterbox-Turbo TTS (expressive/emotion) | 🟢 nicer voice | Med |

**Already done:** Silero VAD (#1 voice fix), Graphiti config fixes, Gemini model fix,
voice latency (energy gate + small→distil STT), identity anchor, async persist.

**Headline:** most of the stack is *well-chosen* (Kokoro, Graphiti, LiteLLM all
validated as current). The two real gaps are **streaming STT** (architecture, not
model) and **embedding quality** — both addressable, both high-value.

---

## Sources
- VAD/turn-taking: [LiveKit turn detection](https://livekit.com/blog/turn-detection-voice-agents-vad-endpointing-model-based-detection), [Silero VAD](https://github.com/snakers4/silero-vad)
- STT: [Moonshine vs Parakeet vs Whisper 2026](https://www.onresonant.com/resources/local-stt-models-2026), [RealtimeSTT](https://github.com/KoljaB/RealtimeSTT)
- LLM: [Best small local LLMs 2026](https://localaimaster.com/blog/small-language-models-guide-2026)
- TTS: [Open-source TTS 2026: Kokoro/Chatterbox](https://www.tryspeakeasy.io/blog/open-source-text-to-speech-2026)
- Memory: [Agent memory frameworks 2026](https://medium.com/@wasowski.jarek/i-compared-5-ai-agent-memory-systems-across-6-dimensions-none-wins-6a658335ed0a)
- Embeddings: [Embedding models 2026 / MTEB](https://www.morphllm.com/ollama-embedding-models)
