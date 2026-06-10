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

## Reactivity (already wired, not yet driven)
`orb.html` polls `GET /api/state` every 200ms and reacts: `state` ∈ idle|listening|speaking + `amplitude`
→ `uBrighten` (core swell + bloom) and `uSurge` (mist warms, frequency line spikes). Server has GET+POST
`/api/state` (`friday/gui/server.py`). **Next step:** have the voice loop (`pipeline.process_audio`) POST
`{state, amplitude}` so the frequency line spikes with her real voice instead of the idle ripple.

## Open next steps
1. Wire voice → `/api/state` amplitude (above) — the main remaining feature.
2. Optional aesthetic tuning Sarjan may still want: mist density/height, frequency line height/mirror, presence-vs-mist balance.
3. Later (deferred, multi-week): the full Tauri + Next.js dashboard (Phase 1.7/1.10) — keep the orb on the stdlib-serve path until then.

## Related
- `friday/gui/server.py` — stdlib http server + chromium kiosk launcher (`_launch_fullscreen`).
- `friday/cli.py` — `sutra orb` command (`--windowed`, `--no-open`).
- `docs/FRIDAY_VISUAL_DIRECTION.md` — original visual spec (palettes/motion).
