# Memory Viewer + Ambient OS-Memory — Build Log

**Date:** 2026-06-03
**Status:** ✅ shipped & pushed (`origin/main`) · 49 tests passing · daemon live

This covers the brain-graph memory viewer and the ambient OS-memory daemon,
step by step, plus the corrections and change-notes from building them.

---

## What we built

### Step 1 — Brain-graph viewer (`sutra graph`)
A custom WebGL visualizer for the Graphiti/FalkorDB memory layer.

- **`friday/memory/graphview.py`** — zero-dependency stdlib `http.server` that
  queries FalkorDB over Cypher and returns `{nodes, links, stats}` JSON.
- **`friday/memory/graphview.html`** — `3d-force-graph` / Three.js render:
  glowing halo-sprite nodes, white-hot cores, directional **particle edges**,
  auto-orbit, glassmorphic HUD, legend filter, search, node-detail panel.
- **Zoom-to-reveal labels** — hub names render as dark "pill" chips that fade in
  by camera distance: clean glowing overview when zoomed out, readable names when
  you zoom into a region. Fade range auto-calibrates to the fitted view.
- **CLI:** `sutra graph` (serves `:8700`, opens a browser).

### Step 2 — Real-memory seeder (`scripts/seed_memory.py`)
Pulls live OS/hardware facts (hostname, Kali kernel, RTX 3080 Ti, RAM, CPU, disk)
plus profile + stack facts and writes them as episodes. Took the graph from 8
smoke-test nodes → 31 real nodes.

### Step 3 — Ambient OS-memory daemon (`sutra ambient`)
The north-star "remember everything from the OS" subsystem in `friday/ambient/`.

- **`config.py`** — allowlisted watch dirs, sensitive-path **denylist**
  (`.ssh`, `.env`, keys), noise filters (caches/builds), cadence knobs,
  kill switch `AMBIENT_ENABLED`, per-source toggle `AMBIENT_SOURCES`.
- **`collectors.py`** — four collectors:
  - `window` — app/document focus + durations (`xdotool` + `psutil`)
  - `files` — edits in watched project folders
  - `git` — commits across watched repos
  - `shell` — terminal commands, **summarized by base command name only**
    (args/secrets are never stored — structural redaction)
- **`daemon.py`** — poll loop + `pynput` idle detection (AFK time isn't logged as
  work) + periodic flush to the memory store; degrades gracefully if memory is down.
- **CLI:** `sutra ambient` (daemon) / `sutra ambient --once` (single pass).

**Design principle:** collectors observe cheaply (no per-event LLM); an aggregator
rolls thousands of raw signals into a *few* episodes per flush, so `qwen2.5:14b`
entity-extraction stays bounded.

---

## Corrections we made

- **Location: Sudbury, not Toronto.** Earlier notes said Sarjan is "based in
  Toronto" — that was wrong. Corrected to **Sarjan is from
  Sudbury, Ontario, Canada** (added to the graph + the seeder + user memory).
  The "Toronto" node remains as the work location.
- **CLI default tier** (earlier work) stayed `medium` (free) — Claude is opt-in.

---

## Bugs fixed & change-notes

- **Kapsule mount bug (the big one).** `3d-force-graph` uses the kapsule pattern:
  it must be called `ForceGraph3D()(element)`, **not** `ForceGraph3D(element)`.
  The wrong form configures the graph but never mounts it — the scene stays empty
  and the canvas is black even though data loads fine. One `()` fixed everything.
- **CDN three.js resolution hell.** Sharing a single `three` instance across
  `3d-force-graph` + addons via importmap kept failing (`three/webgpu`,
  `three/examples/jsm/...`, export mismatches). Fix: let esm.sh ship the graph
  fully self-contained, and load `three` separately just for the glow sprites
  (two instances; basic sprites interop fine via three's `.isX` flags).
- **Glow is sprite-based, not UnrealBloom.** Real postprocessing bloom needs the
  graph's internal three instance (cross-instance risk), so glow is done with
  additive halo sprites instead — reads like bloom, zero coupling.
- **Label legibility.** Iterated: plain text → constant-size text → pill chips →
  zoom-to-reveal. Persistent text on every node fought the glow; revealing names
  by zoom keeps the overview clean.
- **Budget money-leak** (earlier work) — free local/groq/gemini calls now always
  record `$0` via a prefix guard, even on a model-name typo.

---

## Current status (updated 2026-06-03 late)

| Check | Result |
|-------|--------|
| Tests | **50 passing** (`pytest -q`) |
| Lint  | new files clean (`ruff`); only pre-existing nits remain |
| Git   | latest `fde6767` — **4 commits committed locally, not yet pushed** |
| Tiers | Groq + Gemini verified live ($0); fallback chain proven |
| Search| `sutra-searxng` :8080 up; `web_search` verified live |
| Graph | ~50 nodes (entities + episodes, incl. orange `ambient:*`) in FalkorDB |
| Viewer| `sutra graph` → `:8700` → HTTP 200 |
| Voice | **VERIFIED LIVE** — push-to-talk p50 1221ms (see below) |
| Daemon| `sutra ambient` built + verified; currently **stopped** |

---

## Voice — verified live (2026-06-03)

First time the voice loop was ever run with a real human voice. 6 push-to-talk
turns, gemma3:4b local agent. **Every turn under the 1500ms target.**

| Stage | p50 | range |
|-------|-----|-------|
| STT (Whisper large-v3) | 490ms | 434–663 |
| Agent TTFT (gemma3:4b)  | 427ms | 411–475 |
| TTS first audio (Kokoro)| 721ms | 610–807 |
| **Total → first audio** | **1221ms** | 1052–1469 |

**Two real bugs the live test exposed (both fixed + committed in `8d20876`):**
1. **Mic crash** — raw ALSA device rejected 16kHz (`PaErrorCode -9997`); input
   now routes through `pulse` like output. Would have crashed on the first word.
2. **3s cold-start** — `prewarm()` didn't warm the LLM; now it does (cold TTFT
   3016ms → warm ~450ms).

**Still unverified:** VAD voice mode (0 turns logged — only push-to-talk proven).

---

## Gap-closing session — status & pickup (2026-06-03)

Working an ordered gap-closing checklist. **Where we stopped (resume here):**

- **Step 1 — voice fixes committed** ✅ `8d20876`.
- **Step 2 — SearXNG / web_search** ✅ `2c27557`. Stood up the `searxng/searxng`
  container (`sutra-searxng` on `:8080`); `infra/searxng/settings.yml` enables the
  `json` API + disables the limiter; added to `docker-compose.yml`. Verified live:
  `web_search("weather in Sudbury Ontario today")` → Environment Canada / Weather
  Network / AccuWeather. $0/local.
- **Step 3 — Groq (MEDIUM tier)** ✅ live — `GROQ_OK` via `llama-3.3-70b`, $0.
- **Step 4 — Gemini (HEAVY tier)** ✅ `fde6767`. **Bug fixed:** Gemini was pinned
  to the retired `gemini-2.5-flash-preview-04-17` (v1beta now 404s) → corrected to
  `gemini-2.5-flash`. Verified live, $0.
- **Step 5 — fallback chain** ✅ `fde6767`, all $0:
  - HEAVY: no Claude key → falls through to Groq → `FALLBACK_OK`
  - MEDIUM: dead Groq key → falls through to Gemini → `GEMINI_OK`
  - MEDIUM: both providers dead → clean `RuntimeError`, never a fake charge

  *Note:* fallback is **intra-tier** (each tier has its own model chain:
  `MEDIUM=[groq,gemini]`, `HEAVY=[claude,groq]`), not the `MEDIUM→HEAVY→LOCAL`
  cross-tier cascade the plan's wording implied. Cross-tier degrade-to-local lives
  in the voice/agent layer (`fallback_tier`).
- **Step 6 — VAD live test** ⏳ still needs a human at the mic (PTT is proven).
- **Step 7 — safety-net** ✅ `53a792d`. (a) `test_model_name_typo_is_safe` locks the
  budget guard (`claud-haiku` typo → conservative default, never fake $0); (b)
  `scripts/check_seed_idempotency.py` — live: entity nodes 38→38 (0 dupes),
  Episodic 20→29, Sudbury + Toronto distinct.
- **Step 8 — final verification** — 50 tests pass; every tier smoke-tested live.
  Only VAD (Step 6) remains.

**Keys:** Groq + Gemini free keys live in `~/friday-os/.env` (gitignored). The
Gemini key is an `AQ.`-prefixed token (not the classic `AIza` form) — authenticates
fine against `v1beta`.

**Honest real-vs-theoretical (updated):**
- REAL & verified: local voice loop (PTT), memory layer, viewer, ambient daemon,
  `web_search`/SearXNG, **Groq + Gemini cloud tiers, the router fallback chain**,
  50 tests.
- STILL THEORETICAL: VAD voice mode (0 turns), Claude tiers (no `ANTHROPIC_API_KEY`,
  by design — opt-in/paid).

**Fixed:** `.env.example` now defaults `AUDIO_INPUT_DEVICE=pulse` (it was blank, which
re-introduced the raw-ALSA `-9997` mic-crash trap on copy).

---

## How to run

```bash
# bring up the memory backend
sudo docker start sutra-falkordb

# seed real OS/profile facts (one-time / occasional)
python scripts/seed_memory.py

# open the brain viewer
sutra graph

# run the ambient watcher (feeds memory continuously)
sutra ambient            # daemon
sutra ambient --once     # single pass
tail -f ~/.local/share/friday/ambient.log

# measure recall quality (regression baseline before any embedder change)
python scripts/eval_recall.py            # hit@1 / hit@3 / hit@5 on the seed set

# tidy the graph (preview-first — both delete nothing without --apply)
python scripts/clean_memory.py           # noise (garbage transcripts)
python scripts/clean_memory.py --dupes   # near-duplicate episodes (reseeds, echoes)
```

Privacy levers: `AMBIENT_ENABLED=0` (off), `AMBIENT_SOURCES=window,files,git,shell`,
`AMBIENT_WATCH_DIRS=/path:/path`.

### Run ambient as a background service

So it survives reboots/logout instead of needing a live terminal:

```bash
mkdir -p ~/.config/systemd/user
ln -sf ~/friday-os/infra/systemd/sutra-ambient.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sutra-ambient
loginctl enable-linger "$USER"           # keep running after logout
systemctl --user status sutra-ambient
```

Needs `AMBIENT_ENABLED=1` in `.env` (the CLI loads `.env` from the unit's
WorkingDirectory). Logs: `journalctl --user -u sutra-ambient -f` (systemd) and
`~/.local/share/friday/ambient.log` (the daemon's own).

**Recall baseline (2026-06-06, seed set):** hit@1 38% · hit@3 75% · hit@5 88%
(1 miss: "falkordb"). Re-run `eval_recall.py` after any embedder/reranker change
to confirm it didn't regress.

---

## Next / deferred

- **GPS / location tracing** — laptop has no GPS chip; this means WiFi/IP
  geolocation. Deferred to a later phase.
- Ambient polish: window collector needs the live loop to accumulate (≥60s
  threshold); local-LLM summarization for more fluent episodes; ~~run as a systemd
  user service~~ ✅ done (`infra/systemd/sutra-ambient.service`); visually
  distinguish `ambient:*` episodes in the viewer.
