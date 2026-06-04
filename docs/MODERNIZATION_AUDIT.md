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

### 3. Local LLM (voice handler) — ✅ TESTED → keep gemma3:4b
- **Now:** `gemma3:4b` (Ollama).
- **Benchmarked (2026-06-04)** on real voice prompts w/ identity anchor + recalled memory:
  - **gemma3:4b** — ~400ms TTFT, **grounds in memory correctly** ("Your name is Sarjan,
    from Sudbury, Ontario"). Good quality + brevity. ✅ **Winner for our memory-centric use.**
  - **llama3.2:3b** — 2× faster (~200ms!) but **failed memory grounding** ("I don't have
    that information... Sarjan"). Kept as a documented *fast* option for non-memory/tool work.
  - **qwen3:4b / gemma4:e4b** — first looked "broken" (empty replies). **Correction:**
    they're **reasoning models** — they emit 300+ "thinking" tokens before the answer,
    so our 128-token voice cap consumed the budget before any content. **`think:false`
    fixes it entirely** (Gemma 4 then streams clean at ~370ms, *matching* gemma3:4b on
    quality + memory grounding). Not a defect — a config gap on our side.
- **Verdict:** The earlier "you are user, from Earth" failure was the *missing identity
  anchor* (fixed), not the model. For **voice**, keep **gemma3:4b** — equal quality to
  Gemma 4 (think off) at **⅓ the VRAM** (3.3GB vs 9.6GB), which matters in the shared
  voice loop.
- **Gemma 4 (`gemma4:e4b`) — kept for a future TEXT/REASONING tier** with thinking *on*:
  for complex agent queries (not latency/VRAM-bound) its reasoning is a genuine upgrade.
- **Action item:** make the local-model path pass `think:false` so any reasoning model
  (Gemma 4, Qwen3, …) works in voice instead of returning empty.
- **Watch:** **Gemma 3n** (audio-native).

### 4. Text-to-Speech — ✅ GOOD CHOICE (keep) · Chatterbox-Turbo evaluated → defer
- **Now:** `Kokoro-82M` (ONNX, GPU) — warm first-chunk ~74ms, RTF ~0.03, 54 fixed voices, 24kHz.
- **Chatterbox-Turbo (evaluated 2026-06-04):** 350M, MIT, English-only. `pip install chatterbox-tts`;
  `ChatterboxTurboTTS.from_pretrained(device="cuda").generate(text, audio_prompt_path=ref, exaggeration=…)`.
  Real wins over Kokoro: emotion/paralinguistic tags (`[laugh]`/`[cough]`), `exaggeration`/`cfg_weight`
  prosody control, and **voice cloning from a 10s clip** (Kokoro can't clone). Beat ElevenLabs ~63% blind.
  Output is watermarked.
- **Costs that keep it out of the realtime loop:**
  - **Latency:** first-chunk ~472ms + RTF ~0.5 *on an RTX 4090*; our 3080 Ti Mobile is ~40–50% of that →
    expect ~0.7–1.0s first-chunk, RTF ~0.8–1.1. Kokoro is ~74ms warm. Swapping it into the voice path adds
    ~0.5–0.9s onto a ~1.0s round-trip — blowing the <1500ms p50 budget, for terse sub-15-word replies that
    gain nothing from prosody.
  - **Runtime/deps:** it's **PyTorch** (`torch`/`torchaudio`), not ONNX — a second heavy GPU runtime beside
    `onnxruntime-gpu`. The official package has **no streaming** (the 472ms figure is the community
    `davidbrowne17/chatterbox-streaming` fork). English-only.
- **Verdict:** **Keep Kokoro as the default/realtime TTS.** Chatterbox-Turbo is worth wiring as an *opt-in
  expressive backend* (`TTS_BACKEND=chatterbox`) for **non-latency-critical surfaces** only — `sutra think
  --speak`, the HORIZON morning brief/narration, and the real draw, a **cloned Sutra persona voice**. Revisit
  at the persona/GUI phase (Month 3+), where a distinct voice + expressive narration earn their keep.
  *Paper eval: public numbers + this-GPU scaling + API/dependency inspection. A live first-chunk benchmark on
  the 3080 Ti is a ~30-min spike if/when we pick it up.*

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
| ~~3~~ | ~~Voice-LLM trial~~ → ✅ **tested, gemma3:4b validated** | — | done |
| ~~3~~ | ~~Barge-in/interruption~~ → ✅ **implemented** (opt-in, headset-reliable; Silero monitor cuts TTS mid-word) | 🟡 natural turn-taking | done |
| ~~4~~ | ~~Chatterbox-Turbo TTS~~ → 🅿️ **evaluated → defer** to persona/GUI phase (expressive/clone backend, not realtime — see §4) | 🟢 nicer voice | Med |

**Already done:** Silero VAD (#1 voice fix), Graphiti config fixes, Gemini model fix,
voice latency (energy gate + small→distil STT), identity anchor, async persist,
**voice-LLM benchmark (gemma3:4b validated vs llama3.2/qwen3)**,
**barge-in (interrupt-on-speak, guarded against speaker echo)**.

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
