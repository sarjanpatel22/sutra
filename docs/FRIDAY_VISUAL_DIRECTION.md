# Sutra Visual Direction — Research Summary

> Output of the deep-research pass on the GUI signature for Sutra's
> ambient presence. Companion to `FRIDAY_FINAL_SPEC.md`.

---

## Final direction

**One room, two states of light.**

- **AURORA** — working/active state. Cool, technical, JARVIS lineage.
  Cyan and violet ribbons drifting across deep space. Central particle
  presence. Used during coding mode, streaming mode, bounty mode, and
  active conversation.

- **HORIZON** — idle/dawn state. Peaceful, vast, possibility. Sunrise
  gradient at the bottom, deep navy above, one bright star, thin horizon
  line, presence sitting above the horizon. Used at morning wake, idle
  returns (30+ min away), wind-down, and producer mode prep.

The same canvas system. Same wordmark. Same minimal information layer.
The sky just shifts based on time of day, mode, and activity.

---

## Why these two and not the others tried

The conversation iterated through six visual directions:

1. **Basic widgets dashboard** — too generic, no character
2. **Tactical HUD with radar rings** — too busy, "RGB salad"
3. **Refined sophisticated** — too quiet, "expensive empty room"
4. **Death Stranding gameplay HUD** — high energy, saturated, kinetic
   (kept as the *active engagement* mode)
5. **Ambient void with single presence** — premium, restraint as luxury
6. **AURORA + HORIZON + EMBER + CHALK variants** — the winners

The first three failed because they tried to be either too information-dense
(noisy/busy) or too sparse (lifeless). The fourth (gameplay HUD) was
"badass" but exhausting as a 12-hour daily driver. The breakthrough was
realizing the room *itself* should communicate state, not chrome around
the room.

**AURORA + HORIZON together** solve this: they're the same architecture
in different moods. The system knows when to be one or the other. The
user never thinks about it — the room just *is* the right room for
the moment.

---

## Design rules (locked)

### Color discipline

- **AURORA palette**: near-black base `#02030A` → `#0A1428`. Single
  cyan accent in tight range: `#18E0FF` hot → `#00B4D8` mid →
  `#0A7E99` dim. Violet undertone `#6B4AB8` at low opacity for nebula
  depth. White `#EAFCFF` only for hot core.
- **HORIZON palette**: deep navy at top `#030610` → `#0A0F22` →
  navy-violet `#1A1838` → plum `#3A2048` → coral `#A05848` → peach
  `#FFB890` at horizon line. White star `#FFFFFF` with warm halo
  `#FFE8D0`.
- **Reserved alert**: amber `#FFB84D` (warnings) and magenta `#FF3CB4`
  (irreversible action confirmations). Used **only** for those purposes.
- Hierarchy is brightness, not hue. Bright = active. Dim = ambient.

### Typography

- **Wordmark (`SUTRA`)**: Eurostile or Aldrich, weight 200–300, letter-
  spacing 10–16px. The only place display-face type appears.
- **Status labels (`LISTENING`, `GOOD MORNING`)**: same family, smaller,
  even wider letterspacing.
- **All data, numbers, telemetry**: JetBrains Mono or Inconsolata.
  Never Orbitron for body text.

### Negative space

- **70%+ of the canvas is darkness**. Always.
- No panels, no borders, no boxes inside the canvas.
- Information lives at corners at ≤9px in low-opacity (`rgba(74,107,
  120,0.5)` and similar). Visible if you look, invisible if you don't.

### Motion

- **AURORA idle**: ribbons drift left-to-right, ~30s loop. Presence
  breathes. Stars subtly twinkle.
- **AURORA listening**: ribbons accelerate slightly inward toward
  center. Presence brightens. Breath dots ripple outward.
- **AURORA speaking**: room tints brighter on TTS amplitude. Presence
  hot core pulses.
- **HORIZON idle**: ultra-slow sky gradient cycle (full day-night over
  hours, tied to real Toronto sunrise/sunset). Star twinkles.
- **HORIZON listening**: sky brightens 10%, star intensifies, presence
  rises slightly above horizon.
- **Transition AURORA ↔ HORIZON**: ~3 seconds, ribbons dissolve into
  horizon gradient or vice versa. This is the most "JARVIS booting"
  moment in the system.

### Information on demand

Default state is silence. Information is summoned.

- *"Sutra, status"* → aurora pulls aside, single panel materializes,
  hangs 4 seconds, dissolves back
- *"Sutra, full view"* → full gameplay-grade HUD fades up (kept as a
  third "active engagement" mode for heavy work)
- *"Sutra, brief me"* → HORIZON shows the morning brief

---

## Implementation path

### Tech stack

- **Three.js + custom shaders** for the particle systems and gradients
- **GLSL fragment shaders** for the aurora ribbons (3 layers, simplex
  noise displaced)
- **Three.js Points + ShaderMaterial** for the particle field
- **Web Audio AnalyserNode** (fftSize 512) for audio-reactive coupling
- **CSS gradients** for the horizon sky background (cheaper than shader,
  same look)
- **Framer Motion** for state transitions
- **Tauri + Next.js** as the desktop shell

### Performance budget

- Particle count: 12,000–20,000 GPU-driven (well within 16GB VRAM)
- Aurora ribbons: 3 paths with gaussian blur (essentially free)
- Stars: 25–30 SVG circles (free)
- Bloom only on the brightest 10–20% of particles
- Couple only 2–3 global parameters to audio (amplitude, glow, scale)
  — do NOT couple every particle independently

### Build order (within v1 Month 3)

1. **HORIZON static** — gradient + horizon line + star + presence.
   Easiest. Get the look right first.
2. **HORIZON motion** — slow gradient cycle, star twinkle, presence
   breathe.
3. **AURORA static** — base + ribbons + particles + presence.
4. **AURORA motion** — ribbon drift, particle drift, idle behaviors.
5. **State response** — listening, speaking, error states for both.
6. **Transitions** — AURORA ↔ HORIZON, mode-aware switching.
7. **Information surface** — voice-triggered panels appearing/dissolving.

### What NOT to do

- No wireframe head in v1. Wireframe avatar is v2.
- No emotion-classifier-driven color shifts in v1. Static palette per
  mode.
- No procedural galaxy generation. Hand-crafted stars and ribbons.
- No multi-color particle "RGB salad."
- No always-on screen scraping for context.
- No data shown on the canvas that doesn't have a reason to be there.

---

## Reference index

References that shaped the direction:

| Reference | What we took |
|-----------|--------------|
| **Apple Vision Pro environments** | Premium ambient room concept |
| **Blade Runner 2049 (Wallace Corp UIs)** | "Pure, minimal, geometric" = power |
| **Ghost in the Shell 2017 (Territory Studio)** | "Digital sand" voxel forms that coalesce |
| **JARVIS HUD (Jayse Hansen)** | Radial Swiss-army knife composition |
| **Cortana (Halo)** | State encoded in hue/intensity, "numbers and symbols flash when thinking" |
| **Tron Legacy (GMUNK)** | Single-accent monochrome on black, strict grid |
| **Death Stranding / Returnal HUDs** | Saturated kinetic feedback (kept for active mode) |
| **Her (Geoff McFetridge)** | Warm ambient companion, no avatar — *counter-reference*, we are not this |
| **Severance** | Clinical monochrome — *counter-reference*, we are warmer |

---

## What's in scope for v1 GUI

Month 3 of v1 ships:
- HORIZON wake/idle state
- AURORA active state
- State transitions
- Audio-reactive presence
- Voice-triggered panel reveal (memory, council, current task)
- 4–5 widgets that can materialize on demand

What's deferred to v2:
- Wireframe head avatar
- Full Three.js TalkingHead.js lip sync
- Emotion-engine-driven color shifts
- Mode-specific layouts for streaming/bounty (just palette shifts in v1)

---

## The line

**Badass via restraint. Energizing via motion. Simple via discipline.
Premium via what's not there.**

The dashboard's job is not to show you data. It's to be the right room
for whatever you're doing right now. Information appears when summoned.
Until then, the room just breathes.

That's the product.
