# Sutra — Full Live-Test Checklist

Run top-to-bottom to exercise **every** feature in one session. Each line is the exact
command / spoken phrase + what a **PASS** looks like. Tick `[x]` as you go; note anything
that fails and we fix it.

> Tonight's 5 fixes are flagged **★** — verify those especially.

---

## 0. Preflight (30 seconds)
- [ ] `sutra doctor` → all green (Python deps, binaries, skills, services). Fix anything red first.
- [ ] `sutra secrets` → keys resolve from keyring/env, nothing critical "missing".
- [ ] `sutra skills` → ~49 skills listed (no import errors).

## 1. Bring it all up together (the "everything on screen at once")
Open 3 terminals so the whole system is live simultaneously:
- [ ] **Terminal 1** — `sutra dashboard` → orb + chat + live-action feed + telemetry in one window.
- [ ] **Terminal 2** — `sutra voice` → push-to-talk (hold SPACE). This is the core surface.
- [ ] **Terminal 3** — `sutra watch` → ambient "glancing over your shoulder" suggestions.
- [ ] Optional 4th: `sutra orb` (full-screen presence) if you want the orb standalone.

## 2. Voice — the core (PTT, hold SPACE) ★
Say each; PASS = it **acts**, not narrates, and stays in **one** browser window.
- [ ] "open firefox" → app launches (or focuses).
- [ ] "open youtube" → opens as a **tab** in your existing firefox ★ (#39 multi-window).
- [ ] "open github" then "open gmail" → both land as **tabs in the same window** ★.
- [ ] "what's on my screen" → describes the desktop (see_screen) ★ (#24).
- [ ] "switch to firefox" → focuses the window.
- [ ] "close firefox" → closes it.
- [ ] "what time is it" / "how are you" → conversational reply (chat path, local).
- [ ] Talk with the **TV/music on** between phrases → background noise is **ignored**, no junk replies ★ (#42 noise gate).
- [ ] Watch Terminal 2 logs: action turns should say `served by ollama_chat/qwen2.5:7b` → **no rate-limit** even on a burst ★ (#40).

## 3. Text brain — CLI ★
- [ ] `sutra ask "open notepad"` → actually **opens** the editor (doesn't just say it did) ★ (#41 text-path).
- [ ] `sutra ask "what is 17 * 23 + 9"` → correct (uses run_python, not mental math).
- [ ] `sutra chat` → multi-turn; try "open firefox" (acts) and a question (answers). `/exit` to quit.
- [ ] `sutra think "should I use threads or async for an IO-bound scraper?"` → local Gemma-4 reasoning.
- [ ] `sutra council "design a rate limiter for my API"` → multi-agent synthesized answer.
- [ ] `sutra subagents` → lists specialists; `sutra delegate "summarize friday/core/router.py"` → report.
- [ ] `sutra tutor "how does an SQL injection auth bypass work"` → teaches the WHY (mentor tone).

## 4. Desktop control (take-over)
- [ ] `sutra guide "how do I change the wallpaper"` → points at the control, read-only (no click).
- [ ] `sutra do "click the Activities button"` → actually clicks (confirmation-gated; `-y` to skip).

## 5. Web + search
- [ ] `sutra ask "search the web for the latest on CVE-2024-3094"` → web_search (SearXNG) results.
- [ ] In `chat`: "go to tryhackme.com and tell me what's on the landing page" → web_task / browse.

## 6. Memory
- [ ] In `chat`: tell it a fact ("my favorite language is Rust"), then a turn later "what's my favorite language?" → recalls it.
- [ ] `sutra graph` → brain-graph viewer opens (Graphiti/FalkorDB).
- [ ] `sutra rag --help` → ingest a doc, then ask a grounded question against it.
- [ ] `sutra ambient` (briefly) → OS-memory daemon runs, feeds the graph. Ctrl+C to stop.

## 7. Self-extending skills (draft → review → approve)
- [ ] `sutra skill draft "a skill that reverses a string"` → writes an **inert** pending file (does NOT run).
- [ ] `sutra skill list` → shows it pending; `sutra skill show <name>` → review the code + flags.
- [ ] `sutra skill approve <name>` → promotes it; it loads on the next run.

## 8. Pentest stack (authorized — your box / your THM account)
- [ ] `sutra vpn_status` (or `sutra ask "check my vpn status"`) → reports tunnel state.
- [ ] `sutra auto --help` / `sutra pwn --help` → confirm the hands-free loop is wired (needs `SUTRA_AUTONOMOUS=1`).
- [ ] If you start a THM box: `SUTRA_AUTONOMOUS=1 sutra pwn <ip>` → enumerates, asks before risky steps.

## 9. Observability (after using it a bit)
- [ ] `sutra stats` → latency p50/p95 per tier/stage + today's spend.
- [ ] `sutra logs` → recent turns + per-tier / per-surface error breakdown.
- [ ] `sutra budget` → spend vs limits (should be ~$0 — everything ran free/local).
- [ ] `sutra status` → one-shot health check.

## 10. Integrations (optional)
- [ ] `sutra mcp` → connects configured MCP servers, lists their tools.
- [ ] `sutra telegram` → bot starts long-polling (needs TELEGRAM_BOT_TOKEN); message it from your phone.

---

## Tonight's 5 fixes — quick verdict
- ★ **#39 multi-window** — §2: repeated opens = tabs in ONE window.
- ★ **#40 local-first** — §2: logs show `qwen2.5:7b`, no rate-limit on a burst.
- ★ **#41 text-path** — §3: `sutra ask "open notepad"` actually opens it.
- ★ **#42 noise gate** — §2: TV/music between phrases is ignored.
- ★ **#38 qwen3** — anywhere cloud is used, you've got stronger models + more headroom.

**Found a gap?** Note the command + what happened (and grab `sutra logs`), send it over, and I'll root-cause + ship the fix.
