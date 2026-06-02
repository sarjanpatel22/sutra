# Self-Extending Skills — Deep Research

> Research output for the Friday project. Goal: understand what "self-learning
> skills" actually means in 2026, why someone won a hackathon claiming 250+
> skills in 8 hours, and what Friday should actually do.
>
> Sources: ~30 papers, frameworks, and hackathon writeups reviewed. Citations
> inline.

---

## 0. Headline finding

The "250+ skills in 8 hours" claim is **almost certainly not Level 4
Voyager-style autonomous skill discovery.** It's one of three known shortcuts
that produce that volume in that time:

1. **Bulk-installed skill libraries.** A single command (`npx
   antigravity-awesome-skills --claude`) installs 1,234+ Claude Agent Skills.
2. **OpenAPI → MCP auto-generation.** Tools like Stainless, FastMCP, and
   openapi-mcp-codegen convert any REST API with an OpenAPI spec into a
   working MCP server in ~5 minutes. 10–15 popular APIs = 200+ tools in an
   hour.
3. **Dynamic meta-tools.** Stainless dynamic mode exposes 3 meta-tools
   (`list_api_endpoints`, `get_api_endpoint_schema`, `invoke_api_endpoint`)
   that effectively give the agent infinite tools without exploding the
   context window.

None of these solve the **verification problem** — knowing whether a
self-generated skill actually works on a novel task. That problem is still
open research.

The rest of this doc is the actual map of what's solved, what isn't, and
what Friday should do.

---

## 1. The five levels of skill self-extension

A clearer ladder than the original handover's Phase 8:

| Level | What it means | Status in 2026 |
|-------|--------------|---------------|
| **0 — Manual** | Human writes skills with LLM help, reviews every line | Production-ready, day one |
| **1 — Bulk import + auto-scaffold** | Install pre-built skills, generate MCP servers from OpenAPI, scaffold custom skills via LLM | **Production-ready 2026. This is what hackathons demo.** |
| **2 — Pattern-driven proposal** | Agent watches usage logs, proposes new skills from patterns, human reviews & approves | Achievable now with engineering effort |
| **3 — Validated auto-generation** | Agent writes + validates + smoke-tests new skills against held-out validation gates | **Active research. Frontier as of May 2026 (SkillOpt, SEVerA).** |
| **4 — Open-world autonomous skill discovery** | Agent discovers what it needs, finds/writes it, verifies it, deploys it, monitors and deprecates — no human in loop | **Unsolved.** Works in Minecraft (Voyager), doesn't generalize. |

The hackathon was Level 1. The handover's Phase 8 was reaching for Level 4.
Reasonable target for Friday: **Level 1 in v1, Level 2 in v2, Level 3 by v3
on read-only skills only.**

---

## 2. Voyager — the OG, and why it doesn't transfer

Voyager (NVIDIA + Caltech, May 2023, arXiv:2305.16291) is the foundational
paper. It introduced LLM-powered lifelong learning in Minecraft.

**Architecture** (three components, all worth understanding for Friday):

1. **Automatic curriculum** — proposes the next skill to learn based on what
   the agent already has, current state, and exploration progress.
2. **Skill library** — executable code stored with natural-language
   descriptions, embedded for semantic retrieval. Skills are "temporally
   extended, interpretable, and compositional."
3. **Iterative prompting** — uses environment feedback, execution errors, and
   self-verification to refine code over multiple rounds. Self-verification
   is the critical part.

**Results in Minecraft:** 3.3× more unique items, 2.3× longer travel distance,
key tech-tree milestones up to 15.3× faster than prior SOTA. Generalizes
zero-shot to new Minecraft worlds.

**Why it works in Minecraft but not for Friday:**

Voyager's self-verification relies on the Minecraft API to programmatically
check task completion (did you get the diamond? yes/no, inventory query). For
Friday's real-world skills, this property breaks:

| Property | Minecraft | Friday's domain |
|----------|-----------|-----------------|
| Success criterion | Verifiable in-game state | Often subjective, context-dependent |
| Environment | Constrained, forkable | Open, unforkable, has side effects |
| Failure cost | Reset the world | Sent the wrong email, pushed broken code |
| Test data | Generate via random spawns | Hard to generate for arbitrary APIs |
| State space | Bounded | Unbounded |

This is the **verification problem**, and it's the load-bearing wall between
Level 3 and Level 4. Every paper since Voyager has either ignored it,
restricted to constrained environments, or invented a partial solution.

---

## 3. Voyager successors (2023–2025) — incremental progress

### JARVIS-1 (2023)

Extends Voyager's skill-library approach with **multimodal memory** (visual +
textual plans), completing 200+ Minecraft tasks. Skills become visual-textual
plan templates. Still Minecraft-bound.

### SAGE — Self-improving Agent with Skill Library (2024)

Introduces RL-based skill acquisition into the Voyager paradigm. Addresses
the limitation that Voyager's skills are only added on success, not refined
through reward signal. Skills now improve incrementally, not just
binary-pass-or-fail.

### TOOLVERIFIER (2024, arXiv:2402.14158)

Self-verification method for tool generalization to unseen tools. On the
ToolBench benchmark (17 unseen tools across 4 tasks), it achieves a **22%
average improvement over few-shot baselines**. Uses verification questions to
discern between candidate tools and their parameters. This is closer to
Friday's use case — tool selection, not skill creation, but verification is
the key idea.

### Self-driven Grounding / SDG (2023)

LLM agents learn language-aligned skills automatically. Tested on BabyAI
benchmark. Notable for hitting imitation-learning performance with zero
demonstrations.

### LATS, ToolLLM, ToolBench

Various tool-using LLM benchmarks and frameworks. Production-relevant but
not autonomous skill generation — they test how well models *use* given
tools, not how well they *create* them.

---

## 4. The frontier in 2026 — partial solutions to verification

Three papers from 2025–2026 are doing the real work.

### SkillOpt (Microsoft, May 2026, arXiv:2605.23904)

**This is the most important paper for Friday's purposes.**

Treats the skill document as the external state of a frozen agent, and
applies deep-learning-style optimization to it in text space:

- **Validation gate**: an edit is accepted only when it strictly improves a
  held-out validation score
- **Textual learning-rate budget**: bounded add/delete/replace edits per step
- **Rejected-edit buffer**: failed edits become negative feedback
- **Epoch-wise slow update**: longer-horizon stability without weight updates

**Results**: Best or tied on 52/52 evaluated cells across 6 benchmarks, 7
target models, 3 execution harnesses (direct chat, Codex, Claude Code).
**+24.8 points on GPT-5.5 in Codex agentic loop.** Transfers across model
scales and execution environments.

**Status**: open-source at github.com/microsoft/SkillOpt. Production-usable
if you have a benchmark to validate against. Limitation: requires a
held-out validation set per skill domain. Doesn't solve open-world
verification.

### SEVerA — Verified Synthesis of Self-Evolving Agents (March 2026)

Addresses formal safety verification of autonomously synthesized agent
programs. Notes that "modern self-evolving frameworks further complicate
formal safety verification as they fine-tune the parameters of models
embedded in the synthesized programs to improve task-specific performance."

In plain language: when the agent rewrites its own code AND its own weights,
verifying that the result is safe becomes hard. SEVerA proposes a hybrid
verification approach combining LLMs with symbolic planners, theorem
provers, and formal verification layers.

**Status**: paper-stage. Not production-ready. But the direction is
correct: hybrid verification is probably the only way to get safety
guarantees on autonomous skill generation.

### Propose-Solve-Verify (PSV, Wilf et al. 2025)

Formal verification in self-play for code synthesis. Trains LLMs via
self-play without unit-test-based correctness signals (which lead to reward
hacking). Uses formal verification (theorem prover) as the reward signal
instead. Significant pass@1 improvements on code benchmarks.

**Status**: research. Implication for Friday: pure unit-test rewards aren't
enough for autonomous skill generation — the agent learns to game the tests.

### The 2026 paper deluge

The Semantic Scholar similar-papers list for SkillOpt alone surfaces:

- **SkillEvolver**: Skill Learning as a Meta-Skill (2026)
- **SkillGen**: Verified Inference-Time Agent Skill Synthesis (2026)
- **SkillMAS**: Skill Co-Evolution with LLM-based Multi-Agent System (2026)
- **Skills on the Fly**: Test-Time Adaptive Skill Synthesis for LLM Agents (2026)
- **SkillFlow**: Benchmarking Lifelong Skill Discovery and Evolution for Autonomous Agents (2026)
- **PACE**: Two-Timescale Self-Evolution for Small Language Model Agents (2026)
- **ClawTrace**: Cost-Aware Tracing for LLM Agent Skill Distillation (2026)

The field is moving fast. **None of these claim Level 4. They're all
chipping away at Level 3 in narrower domains with verification gates.**

---

## 5. The shortcut path — what hackathons actually use

Skip the research. Use existing infrastructure.

### Anthropic Agent Skills (open standard since March 2026)

`SKILL.md` format — YAML + Markdown definition of a skill, optionally with
Python code, templates, and assets. Folder-based, file-system-discoverable.
Claude auto-loads skills when relevant.

Format example:

```yaml
---
name: standup
description: Daily standup helper. Use when user asks for standup notes.
---
# Standup Skill

Generate yesterday's accomplishments, today's plan, and blockers from:
1. Git commits in the last 24h
2. Slack messages from the user
3. Calendar events
...
```

**Adoption**: 277K+ installs for the official frontend-design skill alone.
Hot-reload added in Claude Code 2.1.0 (Jan 2026). Open standard works
across Claude Code, Cursor, Gemini CLI, Codex CLI.

**For Friday**: write skills as SKILL.md folders, drop them in `/skills/`,
they auto-register. Same format as Anthropic's own internal skills.

### Antigravity Awesome Skills

`npx antigravity-awesome-skills --claude` — installs **1,234+ skills**
at once. Community-curated. This alone explains how someone could claim
"250+ skills" without writing any.

### MCP servers from OpenAPI

Four tools that auto-generate MCP servers from OpenAPI/Swagger specs:

| Tool | Language | Hosting |
|------|----------|---------|
| Stainless | TypeScript | Managed or self-hosted |
| FastMCP | Python | Self-hosted |
| openapi-mcp-codegen | TypeScript/Python | Self-hosted |
| Speakeasy Gram | TypeScript | Managed |

Stainless adapts to client-specific limitations (Cursor's 40-tool cap,
OpenAI's no-root-anyOf rule, Claude Code's array-as-string requirement).

**Stainless dynamic mode** (notable): instead of exposing 100 tools, exposes
3 meta-tools — `list_api_endpoints`, `get_api_endpoint_schema`,
`invoke_api_endpoint`. The LLM discovers and invokes endpoints on demand.
For APIs with >50 endpoints, dynamic mode is default. This is a clean
solution to the context-window problem.

### The 8-hour math

To get 250+ skills working in 8 hours:

```
Bulk install Antigravity skills (5 min)                       ~1,234 skills
Stainless-generate MCP servers for 10 popular APIs (60 min)     ~150 tools
  GitHub, Slack, Notion, Linear, Stripe, Spotify,
  Google Calendar, Gmail, Drive, Postgres
Custom Claude Agent Skills for personal workflows (3 hrs)        ~10 skills
Demo prep + testing + slides (3 hrs)                                    —
                                                              ──────────────
                                                              ~1,400 callable
```

Easy 8-hour build. The agent doesn't autonomously create any of these — a
human wrote configs, ran generators, demoed the result.

**Important**: this is genuinely useful and a totally legitimate hackathon
win. It's just not Level 4. The architecture is "agent has access to lots
of pre-built tools," not "agent grows its own toolkit autonomously."

---

## 6. What Friday should actually do

### v1 (months 1–6): Levels 0–1

**Skill registry pattern with SKILL.md compatibility.**

```python
# Native Python skills (for tight loops, voice handlers)
@skill(
    name="web_search",
    description="Search the web via local SearXNG",
    auto_confirm=True,
)
async def web_search(query: str, max_results: int = 5) -> list[SearchResult]:
    ...

# SKILL.md folder skills (for higher-level workflows, docs, etc.)
# friday/skills/standup/SKILL.md  →  auto-loaded
```

**Bulk-imported skills via:**
- MCP-server clients to Anthropic-spec MCP servers (GitHub, Slack, etc.)
- OpenAPI auto-generation for any API Friday needs (one-time setup per API)
- FastMCP to wrap personal services (Wazuh dashboard, home automation, etc.)

**Target**: 30+ working skills by end of month 2, 60+ by end of v1. No
autonomous generation. All human-written or auto-generated from specs.

### v2 (months 7–12): Level 2

**Pattern-driven proposal.**

- Friday logs usage patterns in the memory graph (Graphiti)
- Background analysis identifies repeated query patterns ("checked Wazuh
  dashboard 4 mornings in a row")
- Friday surfaces a proposal: "Want me to make a `wazuh_morning_brief`
  skill that does X, Y, Z?"
- Approve → Claude generates the SKILL.md → human reviews → merge
- Reject → Friday notes it and stops suggesting that pattern

**Effort**: ~3 weeks of work. High value, low risk. Human is always in the
loop for skill creation.

### v3 (months 13–18): Level 3 (read-only skills only)

**Validated auto-generation.**

Adopt SkillOpt's architecture for skill *refinement* (not initial creation):

- Friday tracks success/failure of each skill over time
- For read-only skills (`auto_confirm=True`), Friday can propose refinements
  to the SKILL.md document
- A validation gate runs the skill against a held-out set of historical
  queries (logged from your usage)
- Edit is accepted only if it beats current performance
- Rejected edits saved as negative feedback

**Hard constraint**: write/send/delete skills NEVER get auto-refinement.
Too risky. Side effects can't be rolled back.

**Effort**: ~6 weeks of work, much of which is infrastructure (held-out
validation set generation, success-criterion tracking, rollback). Real
value-add when you have ≥6 months of usage data to validate against.

### v4+ (post-v3, indefinite): Level 4

**Honest answer: don't.**

The open-world verification problem is unsolved. The papers chipping away at
it (SkillOpt, SEVerA, PSV) all require constrained domains with held-out
validation sets. Real-world skills that send emails, push code, or interact
with arbitrary APIs don't have those properties.

If you genuinely want to push toward Level 4 as a research project (separate
from Friday-the-product), that's a multi-year effort and probably worth a
PhD. Don't bolt it onto Friday.

---

## 7. The verification problem — why this matters

Even setting research aside, here's the practical reason Level 4 is hard:

When a skill is generated autonomously, you need to know:

1. **Does it run?** → easy to check (exec it, catch exceptions)
2. **Does it return the right shape?** → easy (JSON schema validation)
3. **Does it call the right APIs with the right params?** → hard for arbitrary tasks
4. **Does it do the right *thing*?** → hardest, often impossible to verify automatically
5. **Is it safe under all inputs it might see?** → formal verification territory
6. **Will it still work tomorrow when the API changes?** → no automated way to know

Voyager solves 1–4 in Minecraft because the environment IS the verifier. In
the open world, the verifier doesn't exist. SkillOpt-style validation gates
get you partway (refinement against held-out tasks works if the task
distribution is stable). PSV-style formal verification gets you partway
(works for code that has provable properties). Neither solves the general
case.

This is why frontier labs (OpenAI, Anthropic, Google) don't ship Level 4
autonomous skill generation. They could if it worked. It doesn't.

---

## 8. Concrete recommendation for Friday

Update §13 of the final spec from:

> "Self-extending skills (Voyager-style, needs verifiable test harness
> first) — Research-grade, indefinite"

To:

> "**Level 1 in v1** — SKILL.md compatibility + bulk MCP server import +
> OpenAPI auto-generation. Target 60+ skills.
>
> **Level 2 in v2** — Pattern detector in memory graph → skill proposal →
> human review → Claude scaffold → merge. 3 weeks of work.
>
> **Level 3 in v3 (read-only skills only)** — SkillOpt-style validation-gate
> refinement of existing skills. Adopt the open-source SkillOpt code
> directly. 6 weeks of work.
>
> **Level 4 (open-world autonomous discovery)** — unsolved research. Don't
> bolt on. If pursuing seriously, scope as a separate research project on
> top of a stable Friday."

---

## 9. Reading list (prioritized)

For implementing this in Friday:

1. **Anthropic Agent Skills docs** — `platform.claude.com/docs/en/agents-and-tools/agent-skills/overview` — first
2. **Stainless OpenAPI → MCP guide** — `stainless.com/docs/guides/generate-mcp-server-from-openapi/` — second
3. **Voyager paper** — arXiv:2305.16291 — for the curriculum + library + iterative prompting architecture
4. **SkillOpt paper + code** — arXiv:2605.23904, github.com/microsoft/SkillOpt — for v3 implementation
5. **TOOLVERIFIER paper** — arXiv:2402.14158 — for skill-selection verification patterns
6. **JARVIS-1 paper** — for multimodal skill memory ideas
7. **PSV paper** — for the "don't use unit tests as the reward signal" lesson

---

## 10. Hard truths

1. The hackathon winner didn't solve Level 4. Nobody has.
2. Level 1 (lots of pre-built skills) is hugely valuable and what Friday
   should ship in v1.
3. Level 2 (pattern-driven proposal) is the highest-leverage place to spend
   v2 engineering time.
4. Level 3 (validated refinement) is achievable with SkillOpt as the base.
   Restrict to read-only skills.
5. Level 4 stays research. If your goal is a working Friday, ignore it. If
   your goal is publishable research, ignore Friday and write papers.

---

## 11. The shape this changes in the spec

Net delta to the final spec doc:

- **v1 expands** to include MCP server import + OpenAPI auto-gen + SKILL.md compatibility (was previously just hand-written Python skills)
- **v2 gets a concrete Level-2 design** (pattern → proposal → review → merge)
- **v3 gets a concrete Level-3 plan** (SkillOpt fork for read-only refinement)
- **v4+ stays honest** about the research gap

The end result: Friday gets the *practical* benefits of self-extending
skills (many tools available, growing toolkit over time, refining what works)
without claiming the *research* version that doesn't exist yet.

That's the honest answer to your question.
