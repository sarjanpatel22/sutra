# Sutra Model Refresh — June 2026

**Author:** Data/models refresh agent  
**Date:** 2026-06-15  
**Hardware:** RTX 3080 Ti Mobile, 16 GB VRAM, 90 GB disk  
**Constraint:** $0/free-first; no pip install; local Ollama + Groq + Gemini free tiers only

---

## Summary

> **⚠️ Maintainer correction (verified live 2026-06-15, after the agent run).**
> The agent's central recommendation — "remove `llama-4-scout`, it's deprecated" — is
> **WRONG**. A live call to `groq/meta-llama/llama-4-scout-17b-16e-instruct` succeeds and
> it IS in Groq's current `/models` list. It is **`llama-4-maverick`** that Groq
> decommissioned — and maverick is **not referenced anywhere** in the codebase. So
> **NO router or vision-model change is needed.** Scout stays: it's the MEDIUM-tier entry
> AND the free vision model used by guide/watch/desktop/telegram. The benchmark tables
> below are accurate; only the scout-deprecation thread is superseded by this note.

Measure-first verdict: **current picks hold on every tier — no change.** What the
benchmarks actually established:

1. **LOCAL_TOOLS**: `qwen2.5:7b` scores **91%** on the full Sutra tool suite; the newer
   `qwen3:8b` scores only **64%** (its thinking mode breaks tool turns). Keep qwen2.5:7b.
2. **MEDIUM (cloud)**: `llama-3.3-70b`, `gpt-oss-120b`, `llama-4-scout`, `gemini-flash`
   all verified working. No removal.
3. **REASON**: `qwen3:8b` ties `gemma4:e4b` (5/6) at 4.4 GB less VRAM — a *possible*
   future swap, but it needs `<think>`-stripping first; not now.
4. `groq/qwen3-32b` is **REQUEST-TOO-LARGE** for Sutra's 43-skill schema — unusable.

---

## 1. Ollama Models on Disk (current)

```
NAME                       SIZE      CAPABILITIES
gemma3:4b                  3.3 GB    completion (no tools)
qwen2.5:7b                 4.7 GB    completion, tools
qwen2.5:14b                9.0 GB    completion, tools
gemma4:e4b                 9.6 GB    completion, tools, thinking
llama3.2:3b                2.0 GB    completion, tools
nomic-embed-text:latest    274 MB    embedding
qwen3:8b (newly pulled)    5.2 GB    completion, tools, thinking
```

Total on-disk: ~34.6 GB. All fit comfortably in 90 GB available disk.

---

## 2. Research: What's New in June 2026

### 2a. Local Ollama — voice/chat fast tier

| Model | Size | tok/s (16GB RTX) | Notes |
|-------|------|-----------------|-------|
| **gemma3:4b** (current) | 3.3 GB | ~80-120 tok/s when sole model | Fast, no tools. Still best for trivial greetings. |
| phi4-mini:3.8b | ~2.5 GB | ~40 tok/s | Beats 7B on logic/math; no tools per Ollama. |
| llama3.2:3b | 2.0 GB | ~100 tok/s | 39% tool-calling (measured). Good fallback only. |

**Verdict:** gemma3:4b remains the best LOCAL voice/chat model under 4 GB. phi4-mini
is not yet pulled and community benchmarks show it lacks tool-calling support on
Ollama — not a drop-in replacement. No change to Tier.LOCAL.

### 2b. Local Ollama — tool-calling tier

| Model | Size | Tool-calling score (measured) | Notes |
|-------|------|------------------------------|-------|
| **qwen2.5:7b** (current) | 4.7 GB | **91% (30/33)** | Measured live on full Sutra registry |
| qwen2.5:14b | 9.0 GB | **91% (30/33)** | Same score, 2× the VRAM |
| qwen3:8b (candidate) | 5.2 GB | **64% (21/33)** | Measured live — badly misses calc/file/URL skills |
| llama3.2:3b | 2.0 GB | **39% (13/33)** | Measured live — not suitable |

**Key finding:** qwen3:8b's thinking mode causes it to emit `<think>` blocks which
the tool-calling eval expects to see a tool call, but the model instead narrates the
action inside `<think>` and returns nothing in `content`. This is a real failure mode
in Sutra's agent loop. qwen2.5:7b at 91% remains the clear winner.

**Verdict:** No change to Tier.LOCAL_TOOLS. qwen2.5:7b keeps its role.

### 2c. Local Ollama — reasoning tier

| Model | Reasoning score (measured 6Q) | VRAM | Notes |
|-------|-------------------------------|------|-------|
| **gemma4:e4b** (current) | **5/6** | 9.6 GB | Miss: "5 machines" (parser empty ANSWER in thinking block) |
| qwen3:8b (candidate) | **5/6** | 5.2 GB | Identical score; 4.4 GB smaller |
| groq/llama-3.3-70b-versatile (cloud fallback) | **6/6** | cloud | Measured live |
| groq/openai/gpt-oss-120b (cloud fallback) | **6/6** | cloud | Measured live |

**Finding:** qwen3:8b ties gemma4:e4b on reasoning at 5.2 GB vs 9.6 GB — almost half
the VRAM. However, the shared miss on "5 machines/5 widgets" is likely a `<think>`
parsing artefact in the bench script (both are thinking models and the ANSWER parser
gets an empty match). Real reasoning quality is comparable. The 4.4 GB VRAM saving is
real and would let the embedding model co-load without eviction. This is the one
**optional upgrade** worth considering.

**Recommendation (optional, not blocking):** qwen3:8b is a viable swap for REASON
tier if VRAM pressure becomes a problem. Both score identically; qwen3 is 4.4 GB
lighter. The risk is its thinking mode — `_LOCAL_NO_TOOLS` must be updated to include
`"qwen3"` (or strip `<think>` tokens from reasoning responses), same as gemma is
handled today. Leave current pick for now; note as a future optimisation.

### 2d. Local Ollama — embeddings

| Model | Size | MTEB score | Context | Notes |
|-------|------|-----------|---------|-------|
| **nomic-embed-text** (current) | 274 MB | ~62 (English avg) | 2048 | Fast, CPU-ok, 768-dim |
| mxbai-embed-large | 670 MB | ~64 | 512 | Better English MRL; shorter context |
| qwen3-embedding:8b | ~5 GB | 70.58 (multilingual) | 32K | #1 MTEB multilingual; 20× larger |

**Verdict:** nomic-embed-text is the right call for Sutra's English-only RAG + Graphiti
use. qwen3-embedding:8b's gains are on multilingual tasks. The size increase (274 MB →
5 GB) is not justified. No change.

### 2e. Groq free tier — June 2026 status

| Model | Status | TPM (free) | RPD | Tool-calling | Measured |
|-------|--------|-----------|-----|-------------|---------|
| **llama-3.3-70b-versatile** (current) | Active | 6,000 | 14,400 | Yes | Rate-limited after 1st call in bench |
| **openai/gpt-oss-120b** (current) | Active | ~6,000 | ~14,400 | Yes | Rate-limited after 1st call in bench |
| meta-llama/llama-4-scout-17b-16e-instruct | **ACTIVE** (verified live 2026-06-15) | ~6,000 | ~14,400 | Yes (+vision) | Works; in Groq's current /models list. Router MEDIUM + the free vision model. |
| meta-llama/llama-4-maverick-17b-128e-instruct | **DECOMMISSIONED** (verified live: 404) | — | — | — | Not referenced anywhere in the code. |
| groq/qwen/qwen3-32b | Active | ~6,000 | ~14,400 | **FAILS** — REQUEST-TOO-LARGE | Measured — can't fit Sutra's tool schema |

**Key finding (CORRECTED — see the banner up top):** `llama-4-scout` is **ACTIVE** —
verified by a live call and present in Groq's `/models` list. It is `llama-4-maverick`
that was decommissioned, and maverick is not referenced in the code. **No change.**

`qwen3-32b` is interesting but measured as REQUEST-TOO-LARGE — Sutra's full tool
registry (~43 skills) exceeds Groq's 6K TPM context window for Qwen3-32B.
Do not add to MEDIUM.

### 2f. Gemini free tier — June 2026 status

| Model | Status | Notes |
|-------|--------|-------|
| **gemini-2.5-flash** (current) | Active (reduced limits) | 1,500 RPD, ~5 RPM on free |
| gemini-2.0-flash | Still available | Lower capability than 2.5-flash |
| gemini-2.5-pro | **Paid-only since April 1, 2026** | Do not add |

**Measured:** gemini-2.5-flash fired 2/3 tool calls correctly in bench_models.py but
hit `ServiceUnavailableError` on 3 of 6 reasoning questions (rate limit). It is still
useful as a fallback but its free quota tightened ~50-80% in Dec 2025.

---

## 3. Benchmark Summary

### 3a. Cloud brain — bench_models.py (measured 2026-06-15)

| Model | Tool 1 (time) | Tool 2 (RAM) | Tool 3 (web) | Latency | Issues |
|-------|-------------|-------------|-------------|---------|--------|
| groq/llama-3.3-70b-versatile | fired=current_time ✓ | rate-limited | rate-limited | 705ms first | Rate-limits on 2nd+ call |
| groq/openai/gpt-oss-120b | fired=current_time ✓ | rate-limited | rate-limited | 428ms first | Rate-limits on 2nd+ call |
| groq/qwen/qwen3-32b | REQUEST-TOO-LARGE | REQUEST-TOO-LARGE | REQUEST-TOO-LARGE | — | Can't take Sutra's tools |
| gemini/gemini-2.5-flash | fired=current_time ✓ | fired=system_info ✓ | NONE (missed) | 660-1040ms | ServiceUnavailable on heavy load |

### 3b. Reasoning — bench_reason.py (measured 2026-06-15)

| Model | Score | Note |
|-------|-------|------|
| groq/llama-3.3-70b-versatile | **6/6** | Perfect on all 6 questions |
| groq/openai/gpt-oss-120b | **6/6** | Perfect (with and without reasoning_effort=high) |
| gemini/gemini-2.5-flash | **3/3 graded** (3 skipped) | Rate-limited mid-run; correct on all graded |
| ollama_chat/gemma4:e4b (current REASON) | **5/6** | Miss likely parsing artefact on thinking block |
| ollama_chat/qwen3:8b (candidate) | **5/6** | Identical score; 4.4 GB lighter |

### 3c. Local tool-calling — eval_toolcalling.py (measured 2026-06-15)

| Model | Score | Key failures |
|-------|-------|-------------|
| ollama_chat/qwen2.5:7b (current LOCAL_TOOLS) | **91% (30/33)** | clipboard_set, youtube URL, "meaning of life" false-positive recall |
| ollama_chat/qwen2.5:14b | **91% (30/33)** | launch_terminal→run_command, youtube URL, clipboard_set |
| ollama_chat/qwen3:8b (candidate) | **64% (21/33)** | Misses calc, run_command, read_file, focus_window, clipboard_set, URLs |
| ollama_chat/llama3.2:3b | **39% (13/33)** | Not suitable; confuses tool boundaries |

---

## 4. Proposed router.py Diff

**None.** The agent originally proposed removing `llama-4-scout` from MEDIUM, but that
was a misread (scout ≠ maverick). Verified live 2026-06-15: scout works and is in Groq's
current `/models` list, so removing it would delete a working model *and* the free vision
backend (`guide`/`watch`/`desktop`/`telegram` all use it). **No change to `TIER_MODELS`.**

### What was investigated and rejected

| Candidate | Why rejected |
|-----------|-------------|
| qwen3:8b → LOCAL_TOOLS | 64% vs 91% tool-calling. Thinking mode breaks agent tool turns. |
| qwen3:8b → REASON | Ties gemma4:e4b at 5/6. Optional future swap if VRAM pressure grows; needs `_LOCAL_NO_TOOLS` update first. |
| phi4-mini:3.8b → LOCAL | No tools support confirmed. Not smaller enough vs gemma3:4b to matter. |
| groq/qwen3-32b → MEDIUM | REQUEST-TOO-LARGE with Sutra's full tool schema. Unusable. |
| llama-4-maverick → MEDIUM | Deprecated by Groq Feb 2026 (replaced by gpt-oss-120b). |
| mxbai-embed-large → embeddings | Marginal MTEB gain, 2.4× bigger, shorter context. No win. |
| qwen3-embedding:8b → embeddings | 20× size increase for multilingual gains Sutra doesn't need. |
| gemini-2.5-pro | Paid-only since April 2026. Excluded. |

---

## 5. RAG Ingest (Step 4)

Ingested the entire `docs/` directory into a new RAG collection `sutra_docs`:

```bash
.venv/bin/python -m friday.cli rag ingest /home/sarjan/friday-os/docs/ --collection sutra_docs
# Output: Ingested 151 chunks from 13 file(s) → 'sutra_docs' (total 151).
```

**Files ingested (13):**
- FRIDAY_FINAL_SPEC.md, FRIDAY_HANDOVER.md, FRIDAY_REALITY_CHECK.md
- FRIDAY_SKILLS_RESEARCH.md, FRIDAY_VISUAL_DIRECTION.md
- GAP_AUDIT_2026-06-15.md, GUI_ORB_HANDOFF.md
- MEMORY_VIEWER_AND_AMBIENT.md, MODERNIZATION_AUDIT.md
- README.md, ROADMAP_BRAINSTORM_2026.md
- SESSION_HANDOFF.md, SUTRA_PHASES.md

**Verified with query:** "What is Sutra's model routing strategy and which tiers exist?"
→ RAG correctly retrieved and cited SUTRA_PHASES.md + FRIDAY_VISUAL_DIRECTION.md with
accurate information about chat_tier(), Groq 70B, and gemma3:4b routing.

**Embedding model used:** nomic-embed-text:latest (274 MB, local, via Ollama)

---

## 6. Recommendation Summary

| Tier | Current | Proposed | Action |
|------|---------|----------|--------|
| LOCAL (voice/chat) | ollama/gemma3:4b | No change | Current pick is correct |
| LOCAL_TOOLS (offline tool fallback) | ollama_chat/qwen2.5:7b | No change | 91% measured, beats qwen3:8b |
| REASON | ollama_chat/gemma4:e4b | No change (optional: qwen3:8b in future) | Identical score; needs thinking-strip work first |
| CODE | ollama_chat/qwen2.5:14b | No change | 91% measured tool-calling |
| MEDIUM | llama-3.3-70b, gpt-oss-120b, llama-4-scout, gemini-flash | No change | Scout verified ACTIVE 2026-06-15 |
| HEAVY/MAX | claude-sonnet-4-6 / claude-opus-4-8 | No change | Paid tiers unchanged |
| Embeddings | nomic-embed-text | No change | Right size/quality trade-off for English RAG |

**One-line verdict:** **No router change.** Every model currently in the router is
verified active (incl. `llama-4-scout`, tested live 2026-06-15) and is the correct
$0/local-first pick on Sutra's actual skill registry. (`llama-4-maverick` is
decommissioned, but it isn't referenced anywhere in the code.) The lasting win from
this pass is the RAG re-ingest (151 chunks) and the benchmark baselines above.
