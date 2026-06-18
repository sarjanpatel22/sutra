# Sutra Skills Roadmap — the path to a do-anything assistant

The future-task plan for skills. Builds on `FRIDAY_SKILLS_RESEARCH.md` (bulk import,
OpenAPI→MCP, SKILL.md, SkillGen) with a **prioritized, decision-made list**: what to build,
native-vs-MCP, gated-or-not, and in what order. Tailored to Sutra's mission — JARVIS-class,
$0-by-default, runs on Sarjan's box, autonomous-pentest frontier.

## Read first — breadth comes from architecture, NOT raw skill count

Every exposed skill is sent to the model in the tool schema **on every turn**. Past ~50 skills
this bites: worse tool-picking, more tokens per call (against the token-lean rule), and it
breaks smaller models — `qwen3-32b` is already measured as REQUEST-TOO-LARGE at 48 skills
(`MODEL_REFRESH_2026-06.md`). So we scale with three levers, in order:

1. **`http_request` (shipped) + MCP servers** → reach to any API / SaaS with **zero** native
   code and (for MCP) on-demand loading. This is most of "lots of skills."
2. **On-demand skill loading** → register *hundreds*, expose only the task-relevant handful per
   turn. The real unlock for scale without bloat.
3. **Self-extending skills** → Sutra writes + registers a new skill when it needs one. The capstone.

**Rule:** a *native* skill is justified only for **high-frequency, local, or latency-critical**
actions. Everything else → `http_request` or MCP. Anything that mutates / does external I/O /
is irreversible ships **gated** (`auto_confirm=False`).

---

## Phase S0 — Enablers (do these FIRST; they make "lots of skills" safe)

| Item | Why it's first |
|------|----------------|
| **On-demand skill loading / skill groups** | Filter the registry to a task-relevant subset per turn → register hundreds, schema stays small. Removes the ceiling. |
| **MCP connect recipes** (Gmail, Calendar, Slack, GitHub, Home Assistant) | Breadth through the bridge that already exists — no native code, loaded on demand. |
| **OpenAPI→MCP autogen** | Per the research doc: 10–15 APIs ≈ 200+ tools in minutes. |
| **Skill usage tracking** | `turns.jsonl` already records tool names; surface `sutra skills --usage` so we keep what fires and trim dead weight before adding more. |

## Phase S1 — Pentest depth (the active frontier · mostly native tool-wrappers · gated)

| Skill | One-liner | Source |
|-------|-----------|--------|
| `decode` / `encode` | base64 / hex / url / rot13 / jwt — the constant CTF need | native |
| `hash_identify` | name a hash type from its shape | native |
| `hash_crack` | wrap john/hashcat against a wordlist | native (gated) |
| `dns_lookup` / `whois` / `reverse_dns` | recon basics, structured output | native |
| `subdomain_enum` | passive + brute subdomain discovery | native/tool |
| `port_scan` | nmap → **structured** JSON (open ports/services), not raw text | native (scope-guarded) |
| `searchsploit` / `cve_lookup` | exploit-DB + CVE search from a service/version | native/http |
| `payload_gen` | reverse-shell / msfvenom one-liners for a target | native (gated) |
| `web_fuzz` | ffuf/gobuster → structured findings | native (scope-guarded) |
| `forensics` | unzip-crack / steghide / binwalk / strings for CTF artifacts | native |

## Phase S2 — Comms & productivity (the daily-driver gap · mostly MCP)

`email` (read/search/send/draft) · `calendar` (read/create/free-slots) · `tasks`/`reminders` ·
`notes` (Obsidian/Notion/local markdown) · `contacts` · `messaging` (Slack/Discord send).
→ Prefer **MCP servers**; fall back to `http_request` for anything without one.

## Phase S3 — Dev / ops (he's a SWE · native + ssh)

`code_search` (ripgrep across repos) · `read_logs`/`tail` · `db_query` · `docker_ctl`
(ps/logs/restart) · `process_ctl` (list/kill, gated) · `ssh_run` (run on a remote host, gated) ·
`ci_trigger`/`deploy` (gated).

## Phase S4 — Knowledge & media (mixed)

`wikipedia` · `arxiv` · `github_search` · `stackoverflow` · `summarize_url`/`summarize_file`
(formalize over web_extract + LLM) · `translate` · `pdf_read` · `transcribe_media` (reuse Whisper) ·
`image_describe` (reuse vision) · `ocr_file` · `qr_code`.

## Phase S5 — Proactive & self-extending (the "epic" tier)

| Skill | Why it's the endgame |
|-------|----------------------|
| `schedule` / `cron` | "do X at time Y / every day" — proactive Sutra (ties to Activepieces/n8n re-eval) |
| `workflow` recipes | named multi-step chains the council can run |
| **`create_skill` (self-extension)** | Sutra **writes a new Python skill, tests it in a sandbox, and registers it** on demand (SKILL.md / SkillGen pattern). Infinite skills, **zero** standing schema cost — they load only when relevant. **This is what makes it unbeatable.** Heavily gated + sandboxed. |

---

## Decision rules (apply to every new skill)

- **Native vs MCP/http:** native only if high-frequency / local / latency-critical; else MCP or `http_request`.
- **Gating:** mutates state, external I/O, or irreversible → `auto_confirm=False`. Reads → auto.
- **$0 default:** no skill forces a paid tier; pentest tools stay local.
- **Scope-aware:** any skill that hits a network target routes through the engagement scope guard.
- **Earn the slot:** before a native skill lands, check usage — don't spend schema budget on something MCP/http already covers.

## Suggested build order

**S0 enablers → S1 pentest (frontier) → S2 comms (daily driver) → S5 `create_skill` (capstone) → S3/S4 as needed.**
Ship one small committed unit at a time; let real `sutra logs` usage data steer which native skills are worth it.
