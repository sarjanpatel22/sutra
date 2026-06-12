# GUI Presence Orb — Handoff

Pickup doc for the Sutra presence orb. Read this first in a new chat to resume cheaply.

## Status: aesthetic SETTLED ✅ (2026-06-10)
The gold presence is committed and pushed (`ae94fea` on `origin/main`). Art-directed
live on Sarjan's real GPU (RTX 3080 Ti — nvidia drivers now work, real 60fps). Earlier
"1/10" judgments were off washed-out swiftshader screenshots; **always judge live on the GPU.**

## What it is
File: `friday/gui/orb.html` (single file, Three.js 0.160 via unpkg importmap, stdlib server).
- **Palette:** cold champagne / platinum-gold on warm obsidian (`#070502`). NO cyan/violet, NO warm amber.
- **Presence:** 12k-particle core, slow/heavy "breath", glacial spin, restrained UnrealBloom (only the hot center blooms).
- **Gold mist:** soft drifting haze low behind the core (shader noise plane, NOT a particle ocean).
- **Frequency waveform:** row of glowing gold dots tracing a voice-oscilloscope across the lower frame, tapers at edges.
- **Starfield:** faint warm-gold pinpricks.
- All motion is deliberately slow/heavy ("royal, restrained").

## Taste signals (what landed / what didn't)
- ✅ gold-on-black, royal/heavy/restrained, SLOW motion, mist-like (not hard) waves, a frequency/voice element.
- ❌ orbs-as-orbs, black holes, cyan/violet, warm amber, blazing bloom, busy/fast motion.

## Launch / iterate
```bash
sutra orb                 # full-screen ambient (chromium --app, kiosk launcher in friday/gui/server.py)
sutra orb --windowed      # normal browser tab
sutra orb --no-open       # serve only, http://127.0.0.1:8710/
```
**Chromium stability gotcha (cost us time — don't repeat):**
- Use an **isolated profile** (`--user-data-dir=/tmp/sutra-orb-profile`) or chromium hands off to the
  default-profile singleton and exits immediately.
- A **heavy scene crashes the renderer** (the old 33k-point wave-ocean did) → window closes → looks like
  "it keeps reopening" if a keep-alive is restarting it. Keep the scene light. Current scene is fine.
- `--kiosk` can also bail on focus/key events; `--app --start-fullscreen` (no `--kiosk`) is more stable.
- Reload the live window: `DISPLAY=:0.0 xdotool search --name "Sutra" | tail -1` → `windowactivate --sync` + key `F5`.
- Manual stable launch:
  ```bash
  setsid env DISPLAY=:0.0 chromium --user-data-dir=/tmp/sutra-orb-profile \
    --app=http://127.0.0.1:8710/ --start-fullscreen --window-position=0,0 \
    --no-first-run --no-default-browser-check --noerrdialogs \
    --disable-session-crashed-bubble --enable-unsafe-swiftshader >/tmp/sutra-orb-chromium.log 2>&1 & disown
  ```

## Reactivity (wired + validated ✅)
`orb.html` polls `GET /api/state` every 200ms and reacts: `state` ∈ idle|listening|speaking + `amplitude`
→ `uBrighten` (core swell + bloom) and `uSurge` (mist warms, frequency line spikes). Server has GET+POST
`/api/state` (`friday/gui/server.py`). The voice loop drives it: `friday/gui/orb_client.py` `push(state, amplitude)`
(fire-and-forget HTTP, no-ops when no orb runs) is called from `friday/voice/pipeline.py` — **listening** at the
top of the VAD loop, **speaking** + a real-time TTS **loudness envelope** per chunk, **idle** on exit.
Kokoro streams coarse (~sentence) chunks, so a flat per-chunk RMS flatlines the orb; instead `_chunk_envelope()`
computes a 40ms short-time RMS envelope (~25 updates/sec, amplitude *7 calibrated to real Kokoro: p95→1.0,
silences→<0.1) walked by a daemon thread paced to playback. Covered by `tests/test_orb_client.py`.

## Open next steps
1. **Real voice run (the live test):** `sutra voice --mode vad` with the orb up → watch the frequency line trace
   *your* actual speech. Note the far-field-mic caveat in the main handoff (STT hallucinates from across the room;
   use push-to-talk / a close mic). This is part of the **Gate 2 daily-driver shakedown** (`docs/SUTRA_PHASES.md`).
2. Optional aesthetic tuning Sarjan may still want: mist density/height, frequency line height/mirror, presence-vs-mist balance.
3. Optional UX: auto-launch the orb alongside `sutra voice` (today they're separate processes), or a `sutra-orb` systemd user unit for an always-on ambient presence (mirror `infra/systemd/sutra-ambient.service`).
4. Later (deferred, multi-week): the full Tauri + Next.js dashboard (Phase 1.7/1.10) — keep the orb on the stdlib-serve path until then.

## Dashboard (Phase 1.7 MVP, 2026-06-12)
`sutra dashboard` (port 8720) wraps the orb in the working surface: the untouched orb
page rides as an iframe backdrop under glass panels — conversation (real agent turns),
live action feed (tool calls as they fire, gate badges), telemetry (p50/p95 + spend),
and confirmation cards (approve/decline irreversible skills by click; silence declines).
Same stdlib-serve + chromium `--app` path, zero new deps. Files: `friday/gui/bridge.py`
(EventBus + asyncio-loop bridge, confirm Future round-trip), `friday/gui/dashboard.py`
(SSE + POST endpoints), `friday/gui/dashboard.html` (single-file UI, orb palette).

## Related
- `friday/gui/server.py` — stdlib http server + chromium kiosk launcher (`_launch_fullscreen`).
- `friday/cli.py` — `sutra orb` / `sutra dashboard` commands (`--windowed`, `--no-open`).
- `docs/FRIDAY_VISUAL_DIRECTION.md` — original visual spec (palettes/motion).
