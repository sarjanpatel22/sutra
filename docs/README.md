# Sutra (Friday OS) — Documentation Index

> Drop all these into `~/friday-os/docs/` on your Linux box. Start
> with `FRIDAY_FINAL_SPEC.md` — everything else supports it.

---

## Reading order

| # | File | What it is | When to read |
|---|------|------------|--------------|
| 1 | `FRIDAY_FINAL_SPEC.md` | **The master spec.** Every locked decision, every verified number, every architectural choice. | First. Always. |
| 2 | `FRIDAY_VISUAL_DIRECTION.md` | GUI direction — AURORA + HORIZON, palette, motion, build order | When starting frontend work (Month 3 of v1) |
| 3 | `FRIDAY_SKILLS_RESEARCH.md` | Deep research on self-extending skills (5-level ladder, what's solved vs research) | When designing the skill registry and planning v2/v3 |
| 4 | `FRIDAY_REALITY_CHECK.md` | The critique pass that produced the final spec — why things were cut/changed | When you wonder "wait why did we decide X again?" |
| 5 | `FRIDAY_HANDOVER.md` | **Original vision document** (pre-critique). Superseded by the spec, kept for historical reference. | Optional. The spec contains everything important. |
| 6 | `CLAUDE.md` | Project context for Claude Code. Lives in repo root, not docs/. | Auto-read by Claude Code every session. |

---

## File summary

### `FRIDAY_FINAL_SPEC.md` (the master)

20 sections covering:
- Identity (Sutra), vision, hardware baseline
- Full stack (brain, voice, memory, action, frontend)
- VRAM math (verified — 6.9GB always burnt, 9.1GB free)
- Voice latency math (~1.15s local floor, ~1.63s cloud floor)
- Budget math ($20–100/mo depending on tier)
- Timeline reality (6–9 months v1 at sustainable pace)
- Producer mode architecture (coding, streaming, bounty, meeting, deep work)
- Skill registry pattern with credentials/secrets via OS keyring
- Confirmation patterns + permission tiers
- v1 12-week roadmap
- v2/v3 territory
- Hard rules (no autonomous skill learning v1, no proactive layer v1, etc.)
- Pre-code checklist
- Success metrics

### `FRIDAY_VISUAL_DIRECTION.md`

- AURORA (working state) and HORIZON (idle/dawn) — one room, two lights
- Color palettes with hex codes
- Typography rules (Eurostile/Aldrich for wordmark, JetBrains Mono for data)
- Motion language per state
- Three.js implementation path
- Build order within v1 Month 3
- Reference index (Blade Runner 2049, JARVIS, Cortana, Tron, etc.)

### `FRIDAY_SKILLS_RESEARCH.md`

- 5-level skill self-extension ladder
- Voyager paper analysis (why it doesn't transfer to open world)
- SkillOpt (Microsoft, May 2026) — closest production solution
- The "250+ skills in 8 hours hackathon" — explained (it's Level 1 bulk import, not Level 4 autonomous discovery)
- Stainless / FastMCP / OpenAPI-to-MCP shortcuts
- Concrete v1/v2/v3 plan for skill self-extension
- The verification problem and why it's unsolved

### `FRIDAY_REALITY_CHECK.md`

- The first critique of the original handover
- What was wrong/under-scoped in the original plan
- Why the timeline was reset from 4–6 months to 6–9 months
- Why Qwen 32B was rejected for VRAM
- Why self-extending skills (Voyager-style) was deferred
- Why proactive layer was deferred
- The "no avatar in v1" decision
- Hard rules for v1

### `FRIDAY_HANDOVER.md`

- Original vision document
- 9-phase roadmap (later compressed to v1/v2/v3)
- First architecture sketch
- Stack v1 (some items changed in the final spec)

**Note**: The final spec supersedes this. Keep it for context but
don't follow it directly.

---

## What's NOT in these docs

- **PERSONA.md** — you have to write this yourself. The personality of
  Sutra is *your* voice, not mine. Pull up notes for a week, write
  down what you wish a colleague would say, hand those notes to Claude
  Code and let it shape them into the constitution.
- **Code** — zero lines written. Build starts when you run `claude`
  in `~/friday-os/`.
- **Secrets** — API keys, OAuth tokens, etc. go in OS keyring (not
  `.env`, not git).

---

## Pre-code checklist (before writing line 1)

```bash
nvidia-smi                    # confirm GPU model
echo $XDG_SESSION_TYPE        # Wayland or X11
cat /etc/os-release           # distro for systemd later
free -h                       # confirm 32 GB RAM
df -h                         # disk space (~30 GB for models)
```

Decisions to lock:
- [ ] Final name: **Sutra** recommended
- [ ] Claude API monthly cap (start $30)
- [ ] Default council model: **Sonnet** recommended
- [ ] Pace: 10 hrs/wk (6-month v1) or 20 hrs/wk grind (3-month v1)
- [ ] Credentials store: **OS keyring** recommended

---

## First task in the new Claude Code session

```
Read docs/FRIDAY_FINAL_SPEC.md fully. Then run the three hardware
commands. Then let's write friday/core/router.py.
```

That's it. From there it's a real build session.

---

## When in doubt

The final spec is the source of truth. If two docs disagree, the
spec wins. If you can't find something in the spec, it probably
wasn't decided yet — decide it, write it down, move on.

The plan is finished. The product isn't. Go build.
