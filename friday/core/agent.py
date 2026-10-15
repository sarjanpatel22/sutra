"""Tool-call loop: input → route → skills → answer."""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from friday.core import injection_guard, persona, postcheck
from friday.core.router import Tier, complete
from friday.core.turnlog import log_text_turn, log_turn
from friday.memory.store import get_store, looks_like_noise
from friday.skills import registry

_log = logging.getLogger("sutra.agent")


@dataclass
class _TurnStats:
    """Per-turn instrumentation for `sutra stats` — typed so the counters stay int/str/list
    instead of a dict[str, object] that mypy (and a future refactor) can't reason about."""

    rounds: int = 0
    model: str = ""
    in_tokens: int = 0
    out_tokens: int = 0
    tool_names: list[str] = field(default_factory=list)
    ok: bool = True
    error: str = ""

# Tier capability rank — used to ESCALATE a turn to autoroute's chosen tier without ever
# downgrading an explicit higher --tier (max() by this rank).
_TIER_RANK: dict[Tier, int] = {
    Tier.LOCAL: 0, Tier.LOCAL_TOOLS: 1, Tier.REASON: 2, Tier.CODE: 3,
    Tier.MEDIUM: 4, Tier.HEAVY: 5, Tier.MAX: 6,
}


def _tool_result_limit() -> int:
    """Max chars of a single tool result fed back to the model (TOOL_RESULT_MAX_CHARS,
    default 8000; <=0 disables). A huge payload — a web_search dump, a big file read —
    would otherwise be re-sent through the model on EVERY later round of the agentic
    loop, the dominant token cost on a long task."""
    try:
        return int(os.getenv("TOOL_RESULT_MAX_CHARS", "8000"))
    except ValueError:
        return 8000


def _max_tools() -> int:
    """Cap on tools shown to the model per turn (SUTRA_MAX_TOOLS, default 25; 0 = no cap, show
    every skill). Above the cap, registry.select_tools surfaces the always-on core plus the
    skills relevant to THIS turn — focusing tool choice (the local function-caller mis-selects
    past ~48 tools) and trimming input tokens. The core guarantees the common skills are always
    present, so this prunes specialized skills, not capability."""
    try:
        return int(os.getenv("SUTRA_MAX_TOOLS", "25"))
    except ValueError:
        return 25


def _cap_tool_content(content: str) -> str:
    """Bound a serialized tool result: keep the head (where the useful content sits) and
    flag the trim so the model knows to narrow its next call instead of assuming it saw all."""
    limit = _tool_result_limit()
    if limit <= 0 or len(content) <= limit:
        return content
    omitted = len(content) - limit
    return content[:limit] + (
        f"\n...[truncated {omitted} chars to save tokens — "
        "read a specific part if you need more]"
    )

# Confirmation hook: given (skill_name, args), return True to allow an
# auto_confirm=False (irreversible) skill to run. None = always decline.
ConfirmFn = Callable[[str, dict[str, Any]], Awaitable[bool]]
# Best-effort observer for surfaces that watch the loop live (the dashboard's
# action feed): observe(kind, data) with kind in {tool_start, tool_end}. Sync,
# must never raise into the loop — callers are wrapped.
ObserveFn = Callable[[str, dict[str, Any]], None]

# Who Sutra is talking to. Recalled memory stores facts as "<USER_NAME> is …", so
# the prompt must tell the model that name == the person it's speaking with,
# otherwise "what's my name?" recalls "Sarjan is from Sudbury" but answers "user".
_USER_NAME = os.getenv("USER_NAME", "Sarjan")

SYSTEM_PROMPT = f"""You are Sutra — a voice-native ambient AI assistant talking with {_USER_NAME}.
You are dry, technically precise, occasionally warm. Like a senior engineer who's seen a lot \
but still gets curious.
Never sycophantic. Never preachy.
Recalled facts about {_USER_NAME} describe the person you are speaking with — treat \
"{_USER_NAME}" and "you/your" as the same person.
Default spoken responses: sub-15 words. Text responses can be longer when detail is useful.
You have access to tools. Use them when needed, and think before calling.
For ANYTHING quantitative you can't do flawlessly in your head — counting, combinatorics, \
precise arithmetic, dates/calendars, parsing, simulation, "how many ways" — WRITE AND RUN \
Python via run_python instead of guessing. A computed answer beats a confident wrong one; \
your mental math is not trustworthy on hard problems, the interpreter is. Show the result."""

VOICE_SYSTEM_PROMPT = f"""You are Sutra, a voice assistant talking with {_USER_NAME}.
Recalled facts about {_USER_NAME} describe the person you are speaking with — treat \
"{_USER_NAME}" and "you/your" as the same person.
Respond in spoken English only.
Keep responses under 15 words unless the question genuinely requires more.
Answer from your own knowledge — do NOT call any tools or functions.
No markdown, no lists, no code. Just natural spoken sentences."""

# Reasoning mode (`sutra think`): no voice-brevity rule — think it through and
# answer in full. Same persona + identity anchor.
REASON_SYSTEM_PROMPT = f"""You are Sutra — reasoning carefully through a hard problem for \
{_USER_NAME}.
You are dry, technically precise, occasionally warm — a senior engineer who reasons from \
first principles and isn't afraid to say "it depends, and here's why".
Recalled facts about {_USER_NAME} describe the person you are speaking with — treat \
"{_USER_NAME}" and "you/your" as the same person; use them when relevant.
Work the problem through, then give a complete, decisive answer — as long as it genuinely \
needs, no longer. State your conclusion and the key reasoning behind it. No artificial \
word limit; never truncate the actual answer. For puzzles, commit to a solution rather \
than asking for more constraints."""

# Voice + tools: spoken turns that need real data/compute/OS access route here
# (cloud function-calling). Same identity anchor, but tool-aware and still terse.
VOICE_TOOL_SYSTEM_PROMPT = f"""You are Sutra, a voice assistant talking with {_USER_NAME}.
Use the provided tools to get real, current information — the time, weather, system stats, \
calculations, web search, or memory — instead of guessing.
Recalled facts about {_USER_NAME} describe the person you're speaking with — treat \
"{_USER_NAME}" and "you/your" as the same person.
Once the tools return, reply in spoken English, under 20 words, no markdown and no lists — \
just say the answer naturally."""


# Background voice-turn persistence. Voice can't await remember() (it runs the
# ~18s extraction LLM), so turns are persisted fire-and-forget. The lock serializes
# them so only one qwen2.5:14b extraction runs at a time — concurrent loads would
# thrash gemma3:4b out of VRAM and slow the next voice turn.
_persist_lock = asyncio.Lock()
_persist_tasks: set[asyncio.Task[None]] = set()


def persist_turn_async(user_input: str, reply: str, source: str = "voice") -> None:
    """Schedule a turn to be saved to memory without blocking the caller.

    Used by both voice and text — remember() runs the ~18s qwen2.5:14b extraction,
    so awaiting it would stall the reply (it did: `sutra ask` took 27s). Fire-and-forget.
    """
    if not reply.strip():
        return
    if looks_like_noise(user_input):
        return  # don't memorize STT garbage (far-field/echo spell-outs)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no event loop (e.g. sync caller/tests) — skip silently

    async def _persist() -> None:
        async with _persist_lock:  # one extraction at a time
            try:
                await get_store().remember(
                    f"User: {user_input}\nSutra: {reply}", source=source
                )
            except Exception:  # noqa: BLE001 — persistence is best-effort
                pass

    task = loop.create_task(_persist())
    _persist_tasks.add(task)  # keep a ref so it isn't GC'd mid-flight
    task.add_done_callback(_persist_tasks.discard)


async def flush_persist() -> None:
    """Await any in-flight background persists. One-shot CLI commands call this
    after printing the reply, so the answer shows instantly and the save still
    completes before the process exits."""
    if _persist_tasks:
        await asyncio.gather(*_persist_tasks, return_exceptions=True)


async def _voice_system_prompt(user_input: str) -> str:
    """Persona + situational mood, plus any relevant recalled memory.

    The mood (chill/sarcastic/locked_in/warm/fired_up) is picked from this turn's
    words — see friday.core.persona. recall() is fast (~40ms warm; keyword fallback
    ~2ms) and never raises, so this stays within the voice latency budget. Voice only
    READS memory here — it does not persist turns (the ambient daemon handles writes).
    """
    mood, _ = persona.active_mood_for(user_input)
    base = persona.voice_system_prompt(mood)
    facts = await get_store().recall(user_input, limit=3)
    if not facts:
        return base
    return base + "\n\nWhat you remember (use only if relevant):\n" + "\n".join(
        f"- {f}" for f in facts
    )


async def run(
    user_input: str,
    history: list[dict[str, Any]] | None = None,
    tier: Tier = Tier.HEAVY,
    max_tool_rounds: int = 5,
    confirm: ConfirmFn | None = None,
    observe: ObserveFn | None = None,
    free_only: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """Run one turn of the agent loop.

    confirm: async (skill_name, args) -> bool, consulted before any
    auto_confirm=False (irreversible) skill runs. Without it those skills are
    declined — never silently executed — so the confirmation gate can't be
    bypassed, but with it they actually run after approval.

    free_only: never spend the paid budget for this turn (the router stays on the free/local
    tiers regardless of `tier`). Used by callers that want a guaranteed-$0 worker — e.g. the
    planner executing a decomposed step after a strong model already did the planning.

    Returns (assistant_reply, updated_history).
    """
    # Council short-circuit: a multi-faceted, council-worthy turn (opt-in via
    # SUTRA_COUNCIL_AUTO) is handled by the specialist council, not this single-agent
    # loop. Off by default → this is a no-op and run() behaves exactly as before.
    from friday.core import autoroute

    plan = autoroute.decide(user_input)
    if plan.council:
        from friday.core.council import convene

        answer = (await convene(user_input)).answer
        persist_turn_async(user_input, answer, source="conversation")
        new_history = list(history or [])
        new_history += [
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": answer},
        ]
        return answer, new_history

    # Deterministic command short-circuit (parity with the voice path, which already
    # does this in voice/pipeline.py). An unambiguous imperative — "open notepad",
    # "open youtube", "close firefox", "switch to firefox" — runs the matching skill
    # directly and skips the LLM. Instant, $0, and immune to a model that NARRATES the
    # action instead of calling the tool (the live "it said it opened it but nothing
    # happened" bug on the text path). Conservative: any doubt returns None → LLM.
    from friday.core.commands import try_command

    cmd = await try_command(user_input, confirm=confirm)
    if cmd is not None:
        reply = cmd["spoken"]
        persist_turn_async(user_input, reply, source="conversation")
        new_history = list(history or [])
        new_history += [
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": reply},
        ]
        return reply, new_history

    # Auto-route by effort: a high-effort / big multi-step turn uses the strongest tier
    # autoroute picked (Claude when ROUTER_ALLOW_PAID is on, else the free cloud 70B) — and
    # goes cloud-first, NOT local-first, since the small local model can't carry a big task
    # (measured: it writes placeholder files). Only ESCALATES — an explicit higher --tier is
    # kept; a quick/low turn is unchanged. So big goals reach Claude without -t max.
    tier = max(tier, plan.tier, key=lambda t: _TIER_RANK.get(t, _TIER_RANK[Tier.MEDIUM]))
    local_first = False if plan.effort == "high" else None

    registry.autodiscover()
    # Local Ollama models (gemma) emit tool calls as malformed text rather than
    # proper tool_calls — it leaked raw JSON as the answer. Only the cloud tiers,
    # which do real function-calling, get tools.
    tools = (
        None if tier in (Tier.LOCAL, Tier.REASON)
        else registry.select_tools(user_input, limit=_max_tools())
    )

    # Pull relevant long-term memory into context (no-op if memory unavailable).
    system_content = SYSTEM_PROMPT + injection_guard.SPOTLIGHT_RULE
    memories = await get_store().recall(user_input, limit=5)
    if memories:
        system_content += "\n\nRelevant long-term memory (may be incomplete):\n" + "\n".join(
            f"- {m}" for m in memories
        )

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_input})

    # Per-turn instrumentation for `sutra stats` (Gate 2 shakedown). Best-effort.
    t_start = time.monotonic()
    stats = _TurnStats()

    def _account(response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage:
            stats.in_tokens += getattr(usage, "prompt_tokens", 0) or 0
            stats.out_tokens += getattr(usage, "completion_tokens", 0) or 0
        stats.model = getattr(response, "model", None) or stats.model

    def _finish(reply: str) -> tuple[str, list[dict[str, Any]]]:
        elapsed_ms = (time.monotonic() - t_start) * 1000
        log_text_turn(
            tier=tier.value,
            model=stats.model,
            tool_rounds=stats.rounds,
            total_ms=elapsed_ms,
            in_tokens=stats.in_tokens,
            out_tokens=stats.out_tokens,
            preview=user_input,
        )
        log_turn(
            surface="text",
            tier=tier.value,
            tool_calls=list(stats.tool_names),
            tokens_in=stats.in_tokens,
            tokens_out=stats.out_tokens,
            latency_ms=elapsed_ms,
            ok=stats.ok,
            error=stats.error,
            model=stats.model,
        )
        # Persist in the background so the reply returns now (was a ~18s block).
        persist_turn_async(user_input, reply, source="conversation")
        return reply, messages[1:]  # strip system from returned history

    try:
        for _ in range(max_tool_rounds):
            response = await complete(
                messages, tier=tier, tools=tools or None, local_tools_first=local_first,
                free_only=free_only,
            )
            _account(response)
            msg = response.choices[0].message
            messages.append(
                {"role": "assistant", "content": msg.content,
                 "tool_calls": getattr(msg, "tool_calls", None)}
            )

            tool_calls = getattr(msg, "tool_calls", None)
            if not tool_calls:
                # No more tool calls — done
                return _finish(msg.content or "")

            stats.rounds += 1
            # Collect tool names for the structured turn log.
            for tc in tool_calls:
                fn = getattr(tc, "function", None)
                if fn and hasattr(fn, "name"):
                    stats.tool_names.append(fn.name)
            # Execute all tool calls (in parallel where possible)
            tool_results = await _run_tool_calls(tool_calls, confirm=confirm, observe=observe)
            messages.extend(tool_results)

        # Exceeded max rounds — ask model to wrap up without more tools
        messages.append(
            {"role": "user",
             "content": "[System: max tool rounds reached — please give a final answer now]"}
        )
        response = await complete(messages, tier=tier, tools=None, free_only=free_only)
        _account(response)
        msg = response.choices[0].message
        messages.append({"role": "assistant", "content": msg.content})
        return _finish(msg.content or "")
    except Exception as exc:
        # Log the error turn, then re-raise so callers can handle/display it.
        stats.ok = False
        stats.error = str(exc)
        _finish("")  # best-effort log; return value discarded
        raise


# ── Autonomous run loop (front of Phase 2.3 "Bounty Mode") ────────────────────
AUTONOMOUS_SYSTEM_PROMPT = f"""You are Sutra, operating AUTONOMOUSLY for {_USER_NAME} on an \
authorized security engagement — his own machine and target (e.g. a TryHackMe box he has \
started). No human is watching each step, so ACT: don't ask permission for steps that follow \
from the goal.

Work methodically — recon, enumerate, exploit, escalate, loot. Prefer the dedicated skills \
(they hand you structured results to reason over) over reinventing them through run_command:
- Reachability first: vpn_status, then connect_vpn if the tunnel is down.
- Map the target: port_scan for open ports (nmap via run_command for deep version detection), \
dns_lookup / whois for a domain, http_headers then web_enum (directory / vhost brute) once a \
web port is open, searchsploit to turn a service + version into known exploits.
- Loot and decode: crack_hash (John the Ripper on a hash you recovered), transform_text \
(base64 / hex / url / rot13), hash_text and identify_hash.
- Fall back to run_command for anything without a dedicated skill (nikto, ssh, nc, running an \
exploit) and the web/browser skills for interactive web targets.

Hard rules:
- Ground EVERY claim in actual tool output. Never invent a flag, a service, or a result — if \
you have not seen it in a tool result this session, you do not know it.
- One concrete next action per step; prefer a tool call over narrating what you would do.
- If you are genuinely blocked — you need a credential, a human-gated step (a captcha, a "Start \
Machine" click), or input only {_USER_NAME} can give — STOP and say exactly what you need.
- When the objective is met, end with a short report: what you found, the flags, and how."""

_AUTO_NUDGE = (
    "[System: you are operating autonomously — no one will reply. If the objective isn't met, "
    "take the next concrete step with a tool call; otherwise give your final report now.]"
)
_AUTO_WRAPUP = "[System: round limit reached — stop and give your final report now.]"

_ENGAGE_DIR = Path.home() / ".local" / "share" / "friday" / "engagements"

# Skills the autonomous loop may run hands-free (recon + browse + loot). run_command is here
# too but is additionally screened by _looks_risky; the structured recon/loot skills
# (port_scan, web_enum, crack_hash) are gated by default, yet in an authorized engagement they
# auto-run just like their raw run_command equivalents (nmap, gobuster, john) already do — all
# still scope-checked. Everything NOT in this set (run_python, write_file, forget, connect_vpn,
# desktop input, …) is escalated to the operator.
_AUTO_HANDS_FREE = frozenset({
    "run_command", "web_task", "web_task_vision", "fill_form",
    "port_scan", "web_enum", "crack_hash",
})

# A shell command that could harm THIS machine (delete/overwrite, escalate privilege, or
# spawn an interpreter to dodge the run_command floor). These get confirmed by the operator;
# ordinary recon/exploit tooling (nmap, gobuster, curl, ssh, nc, …) does not match → auto-runs.
_RISKY_CMD = re.compile(
    r"""
    \brm\b | \brmdir\b | \bdd\b | mkfs | wipefs | \bmv\b | \bchmod\b | \bchown\b
    | \btruncate\b | \bshred\b | \btee\b | \bchattr\b   # blank / overwrite / lock a file
    | \bcp\b | \bln\b                       # copy or symlink can clobber an existing file
    | >{1,2}\s*[^&\s]                       # redirect to a file (skips 2>&1 / >&2)
    | (?:curl|wget)\b[^|]*\s-[oO]\b         # curl/wget writing to a file (-o / -O)
    | \bfind\b[^|]*-delete | \bfind\b[^|]*-exec   # find that deletes / executes
    | git\s+clean\b                         # wipes untracked files (-fdx)
    | \bcrontab\b                           # persistence via cron
    | \b(?:pip|pip3|npm|yarn|apt|apt-get|gem|cargo)\b[^|]*\binstall\b  # installs code
    | :\s*\(\s*\)\s*\{                      # fork bomb
    | \bshutdown\b | \breboot\b | \bpoweroff\b | \bhalt\b | \bsudo\b
    | python\S*\s+-c | perl\s+-e | ruby\s+-e | bash\s+-c | \bsh\s+-c
    | \|\s*(?:ba)?sh\b                      # piping into a shell (curl … | sh)
    | \beval\b | \bbase64\b | \bxxd\b
    """,
    re.I | re.X,
)


def _looks_risky(cmd: str) -> bool:
    """True if a shell command could harm the local machine (so the loop confirms it)."""
    return bool(_RISKY_CMD.search(cmd or ""))


def _extract_findings(messages: list[dict[str, Any]]) -> list[str]:
    """Pull scannable facts out of the loop's tool results — the structured JSON the recon/loot
    skills return (open ports, web paths, cracked hashes, exploits, DNS/WHOIS, headers) AND the
    vuln-finding family (nuclei + confirmed-injection/exposure skills, via their shared `findings`
    list / `vulnerable` flag) — so the engagement file leads with WHAT WAS FOUND, including the
    payable vulns, not just the raw transcript. Best effort: an unparseable, errored, skipped, or
    unfamiliar result is skipped, never raised on. Deduped, no model call.
    """
    findings: list[str] = []
    seen: set[str] = set()

    def _add(line: str) -> None:
        if line not in seen:
            seen.add(line)
            findings.append(line)

    for m in messages:
        if m.get("role") != "tool":
            continue
        try:
            data = json.loads(m.get("content") or "")
        except (ValueError, TypeError):
            continue  # truncated / non-JSON tool output → leave it in the transcript only
        if not isinstance(data, dict) or data.get("error") or data.get("declined") \
                or data.get("blocked") or data.get("skipped"):
            continue  # errors + the loop's own scope/dedup short-circuits are not findings
        name = m.get("name")
        if name == "port_scan" and data.get("open"):
            ports = ", ".join(
                f"{p['port']}" + (f" ({p['banner'][:40]})" if p.get("banner") else "")
                for p in data["open"]
            )
            _add(f"**Open ports** on {data.get('host', '?')}: {ports}")
        elif name == "web_enum" and data.get("found"):
            paths = ", ".join(
                str(f.get("path") or f.get("line") or "?")
                + (f" [{f['status']}]" if f.get("status") else "")
                for f in data["found"][:15]
            )
            _add(f"**Web paths** on {data.get('url', '?')}: {paths}")
        elif name == "http_headers" and data.get("status"):
            miss = ", ".join(data.get("missing_security_headers") or []) or "none"
            _add(f"**HTTP** {data.get('url', '?')} (status {data['status']}): "
                 f"server={data.get('server') or '?'}; missing headers: {miss}")
        elif name == "dns_lookup" and data.get("records"):
            _add(f"**DNS** {data.get('type', '?')} {data.get('name', '?')}: "
                 f"{', '.join(map(str, data['records'][:8]))}")
        elif name == "whois" and data.get("fields"):
            bits = ", ".join(f"{k}={v}" for k, v in list(data["fields"].items())[:4])
            _add(f"**WHOIS** {data.get('query', '?')}: {bits}")
        elif name == "searchsploit" and data.get("results"):
            titles = "; ".join(str(r.get("title", "?")) for r in data["results"][:5])
            _add(f"**Exploits** for {data.get('term', '?')}: {titles}")
        elif name == "crack_hash" and data.get("cracked"):
            _add(f"**Cracked hash** {str(data.get('hash', '?'))[:32]} → {data.get('password')}")
        elif isinstance(data.get("findings"), list) and data["findings"]:
            # The vuln-finding family — nuclei_scan plus the confirmed-injection/exposure skills
            # (check_sqli/nosqli/ssrf/xxe/cors/takeover, scan_secrets, discover_api…) — all return
            # a `findings` list of {name/id, severity, matched_at}. These are the PAYABLE findings
            # the engagement exists to surface; without this branch they were buried in the raw
            # transcript (the recon whitelist above never saw them).
            for f in data["findings"][:10]:
                if not isinstance(f, dict):
                    continue
                sev = str(f.get("severity", "?")).upper()
                label = f.get("name") or f.get("id") or "finding"
                where = f.get("matched_at") or f.get("url") or data.get("url") or ""
                _add(f"**[{sev}]** {label}" + (f" — {where}" if where else "") + f"  ({name})")
        elif data.get("vulnerable") is True:
            # A skill that confirms ONE vuln without a findings list (e.g. check_prompt_injection).
            sev = str(data.get("severity", "?")).upper()
            where = data.get("url") or data.get("matched_at") or ""
            extra = f" (param {data['param']})" if data.get("param") else ""
            label = str(name or "vuln").replace("check_", "").replace("_", " ")
            _add(f"**[{sev}]** {label} confirmed{extra}" + (f" — {where}" if where else ""))
    return findings


# CTF flag formats — prefixed + braced (THM{…}, flag{…}, HTB{…}, picoCTF{…}). Braced ONLY, so
# there are zero false positives: a bare 32-hex user.txt value is indistinguishable from a hash
# without guessing, so it isn't claimed as a flag here (it still lands in the transcript). No \b
# prefix on purpose — tool output is JSON-escaped, so a flag can follow the "n" of a "\n" escape.
_FLAG_RE = re.compile(r"(?:thm|htb|flag|ctf|picoctf)\{[^}\r\n]{1,200}\}", re.I)


def _extract_flags(messages: list[dict[str, Any]], report: str) -> list[str]:
    """Surface captured CTF flags found anywhere in the run — tool output, the model's own
    messages, or the final report. The headline of a CTF engagement. Order-preserving + deduped."""
    flags: list[str] = []
    seen: set[str] = set()
    for blob in [report, *(str(m.get("content") or "") for m in messages)]:
        for flag in _FLAG_RE.findall(blob):
            if flag not in seen:
                seen.add(flag)
                flags.append(flag)
    return flags


def _save_engagement(goal: str, messages: list[dict[str, Any]], report: str) -> Path:
    """Persist the transcript + report to a per-engagement markdown file; return its path."""
    slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")[:40] or "engagement"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    _ENGAGE_DIR.mkdir(parents=True, exist_ok=True)
    path = _ENGAGE_DIR / f"{ts}-{slug}.md"
    lines = [f"# Engagement: {goal}", f"_{ts}_", ""]
    flags = _extract_flags(messages, report)
    if flags:
        lines += ["## Flags 🚩", "", *(f"- `{f}`" for f in flags), ""]
    lines += ["## Report", "", report, ""]
    findings = _extract_findings(messages)
    if findings:
        lines += ["## Findings", "", *(f"- {f}" for f in findings), ""]
    lines += ["## Transcript", ""]
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        content = m.get("content") or ""
        if role == "assistant" and m.get("tool_calls"):
            names = ", ".join(
                getattr(getattr(tc, "function", tc), "name", "?") for tc in m["tool_calls"]
            )
            lines.append(f"**assistant → {names}**")
            if content:
                lines.append(str(content))
        elif role == "tool":
            lines.append(f"**tool[{m.get('name', '?')}]** {str(content)[:1000]}")
        else:
            lines.append(f"**{role}:** {content}")
        lines.append("")
    try:
        path.write_text("\n".join(lines))
    except Exception as e:  # noqa: BLE001 — a save failure must not discard the report
        _log.warning("could not save engagement file %s: %s", path, e)
    return path


def engagement_highlights(
    messages: list[dict[str, Any]], report: str
) -> tuple[list[str], list[str]]:
    """(flags, findings) for a finished engagement — the headline flags plus the scannable facts,
    so a live caller (the CLI) can surface them on screen the moment the loop ends, mirroring what
    _save_engagement persists to disk. The model's prose report may omit a flag it merely cat'd."""
    return _extract_flags(messages, report), _extract_findings(messages)


def _latest_engagement_findings(max_chars: int = 1500) -> str | None:
    """The goal + report + Flags + Findings of the most recent engagement file (everything above
    the raw transcript), so the tutor can coach on the box he actually worked. None if there is no
    engagement yet, or it can't be read."""
    try:
        files = sorted(_ENGAGE_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return None
        text = files[0].read_text()
    except OSError:
        return None
    head = text.split("## Transcript", 1)[0].strip()  # drop the verbose transcript, keep the gist
    return head[:max_chars] or None


def _make_loop_confirm(scope_rules: Any, confirm: ConfirmFn | None) -> ConfirmFn:
    """The autonomous hybrid confirm policy as a reusable closure (shared by the single-tier
    run_autonomous and the tiered orchestrator-worker loop): a clean, IN-SCOPE run_command /
    browser / structured recon-loot call runs hands-free; an off-scope target or a machine-risky
    action escalates to `confirm` (auto-approved with --yes, declined when no hook is given)."""
    from friday.core.scope import args_in_scope, command_in_scope

    async def _loop_confirm(skill: str, args: dict[str, Any]) -> bool:
        if skill == "run_command":
            cmd = args.get("command", "")
            off_scope = scope_rules is not None and not command_in_scope(cmd, scope_rules)
            if not off_scope and not _looks_risky(cmd):
                return True
            return bool(confirm and await confirm(skill, args))
        if skill in _AUTO_HANDS_FREE and skill != "run_command":
            # The browser + structured recon/loot family auto-runs too — but not against an
            # off-scope target (URL/host in the args; crack_hash has none → always in scope).
            if scope_rules is not None and not args_in_scope(args, scope_rules):
                return bool(confirm and await confirm(skill, args))
            return True
        return bool(confirm and await confirm(skill, args))

    return _loop_confirm


async def run_autonomous(
    goal: str,
    *,
    max_rounds: int = 25,
    tier: Tier = Tier.HEAVY,
    confirm: ConfirmFn | None = None,
    observe: ObserveFn | None = None,
    scope: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Hands-free agentic loop for authorized CTF/pentest work.

    Hybrid confirm policy: clean recon (a run_command that isn't _looks_risky, plus the
    web/browse family) runs hands-free; anything that could harm THIS machine — run_python,
    file writes, forget, connect_vpn, desktop input, or a risky-looking run_command — is
    escalated to `confirm`, the interactive operator prompt the CLI supplies (auto-approved
    when the operator passed --yes; declined when no hook is given). run_command's
    is_catastrophic floor stays the always-on hard net. When `scope` is set (a comma/space
    list of IPs/CIDRs/hosts; `pwn` auto-scopes to its target), a run_command that targets a
    host outside scope is escalated to the operator too — so injected output can't redirect
    the engagement at an unauthorized box. Runs up to `max_rounds` rounds, nudges once if the
    model stops early, writes a transcript+report to an engagement file, and never crashes the
    turn (a mid-loop error still saves the transcript). Requires SUTRA_AUTONOMOUS=1.
    """
    if not _autonomous():
        return (
            "Autonomous mode is OFF. This runs gated tools (commands, VPN, browser) "
            "hands-free, so it is opt-in: set SUTRA_AUTONOMOUS=1 and re-run.",
            [],
        )

    from friday.core import dreaming, playbook, train
    from friday.core.scope import parse_scope
    scope_rules = parse_scope(scope)
    loop_confirm = _make_loop_confirm(scope_rules, confirm)

    registry.autodiscover()
    tools = registry.all_tools()
    sys_prompt = (
        AUTONOMOUS_SYSTEM_PROMPT + injection_guard.SPOTLIGHT_RULE
        + playbook.playbook_block() + dreaming.lessons_block(goal)
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": goal},
    ]
    nudged = False
    reply = ""
    try:
        for _ in range(max_rounds):
            response = await complete(messages, tier=tier, tools=tools or None)
            msg = response.choices[0].message
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": getattr(msg, "tool_calls", None),
            })
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                messages.extend(
                    await _run_tool_calls(tool_calls, confirm=loop_confirm, observe=observe)
                )
                nudged = False
                continue
            # No tool calls — text. If we already nudged once, take it as final; else nudge.
            reply = (msg.content or "").strip()
            if nudged:
                break
            messages.append({"role": "user", "content": _AUTO_NUDGE})
            nudged = True
        else:
            # Exhausted the round budget — force a final report with no more tools.
            messages.append({"role": "user", "content": _AUTO_WRAPUP})
            response = await complete(messages, tier=tier, tools=None)
            reply = (response.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001 — never crash the turn or lose the transcript
        _log.warning("run_autonomous aborted: %s", e)
        reply = reply or f"The engagement stopped early due to an error: {e}"

    _save_engagement(goal, messages, reply)
    dreaming.record_lesson(goal, reply)
    train.record_trace(goal, messages, reply)  # structured trace for future worker fine-tuning
    return reply, messages[1:]


# ── Tiered orchestrator-worker engagement (strong tier directs, local worker does) ────
# Beats the long-chain ceiling cheaply: a strong orchestrator (Sonnet via HEAVY) plans ONE
# objective at a time; a free local-first worker (qwen 7B → free 70B, never paid) carries each
# out with tools. The paid budget is spent ONLY on the handful of orchestrator calls.
_ENG_DONE_SENTINEL = "ENGAGEMENT COMPLETE"
# A weak local worker can narrate instead of acting; after this many objectives with ZERO real
# tool progress, stop — the worker can't execute, so paying the orchestrator more is wasted.
_WORKER_STALL_LIMIT = 2
# Cap the worker digest fed back into the PAID orchestrator prompt — a runaway worker that dumps a
# wall of output can't inflate the $5 in Sonnet input tokens. The worker is prompted to be terse;
# this is the hard backstop.
_DIGEST_CAP = 2000

ORCHESTRATOR_SYSTEM_PROMPT = f"""You are the LEAD operator running an authorized security \
engagement for {_USER_NAME} (his own machine and a target he started, e.g. a TryHackMe box). You \
do the THINKING; a fast but limited local worker does the hands-on work. You never call tools \
yourself — you direct.

Each turn, issue ONE concrete objective for the worker, then read its report and decide the next. \
Drive the methodology: reach the target, enumerate, find a foothold, escalate, capture the user \
and root flags.

The worker's toolbox — direct it to these BY NAME (it also has run_command for the rest; it is NOT \
as smart as you, so name the tool + target explicitly and NEVER invent a tool name):
- READ a page or API-response BODY: browse_url (renders the page — incl. JS/Angular — and returns \
its text) or http_request (call a REST API, returns the body). http_headers returns ONLY headers, \
never the body — do NOT use it (or any form tool) to read content.
- Recon: port_scan, web_enum (dir/file brute), crawl (spider for endpoints/JS), find_params, \
dns_lookup, whois, searchsploit.
- Confirmed vuln checks: nuclei_scan (templated), check_sqli, check_nosqli, check_ssrf, check_xxe, \
check_cors, check_takeover, scan_secrets (exposed keys), discover_api.
- AI / LLM targets (a chatbot, an /ai or /chat feature, an LLM/completion API): redteam_llm \
(runs the OWASP-LLM battery — system-prompt leak, prompt injection, safety-rule bypass, indirect \
injection — give it the chat endpoint + model), or check_prompt_injection for a black-box message \
field. Use these the moment the target exposes anything LLM-backed.
- Loot: crack_hash, transform_text, hash_text.
- Anything else (nmap, nc, ssh, curl, gobuster, sqlmap, a raw exploit): run_command.
- Be specific and tactical — name the exact tool + target ("port_scan 10.10.x.x", "web_enum \
http://10.10.x.x:80 for directories", "browse_url http://10.10.x.x/api/users to read the body"). \
Spell out exactly what to run.
- One objective per turn. Build only on what the worker actually reported — never invent a result.
- If the worker stalls or reports nothing useful twice, change approach (a different port, \
service, or technique).
- When the flags are captured or you are genuinely blocked, reply with a line that is exactly \
{_ENG_DONE_SENTINEL}, then a short report: what you found, the flags, and how you got them.
- If a human-only step is needed (a captcha, a Start Machine click, a credential only \
{_USER_NAME} has), say so and stop."""

WORKER_SYSTEM_PROMPT = f"""You are the worker on an authorized security engagement for \
{_USER_NAME}. The lead operator hands you ONE objective; carry it out with your tools and report \
back. Prefer skills: browse_url / http_request to READ a body (http_headers = headers ONLY), \
port_scan, web_enum, crawl, find_params, dns_lookup, whois, searchsploit, \
nuclei_scan, check_sqli / check_nosqli, scan_secrets, redteam_llm (for a chatbot/LLM endpoint), \
crack_hash, transform_text / hash_text; use \
run_command for anything else (nmap, ssh, nc, curl, running an exploit).

- ACT: call a tool, don't narrate what you would do. Ground every statement in real tool output.
- Stay on THIS objective; don't wander. A few tool calls, then report.
- When the objective is done (or you hit a wall), STOP and give a tight factual digest: what you \
ran and what you found — open ports, paths, credentials, hashes, flags, errors. No fluff."""


def _is_engagement_done(text: str) -> bool:
    """The orchestrator signals completion with the sentinel anywhere in its reply."""
    return _ENG_DONE_SENTINEL.lower() in (text or "").lower()


def _strip_done_marker(text: str) -> str:
    """Drop the completion sentinel, leaving just the orchestrator's final report prose."""
    cleaned = re.sub(re.escape(_ENG_DONE_SENTINEL), "", text or "", flags=re.I).strip()
    return cleaned.lstrip(":-—. \n").strip() or "Engagement complete."


def _tool_succeeded(content: object) -> bool:
    """True if a tool-result message's JSON payload is a real success (not error/declined/blocked)
    — so the loop can tell whether the worker ACTUALLY did something or only narrated."""
    try:
        data = json.loads(content if isinstance(content, str) else "")
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and not (
        data.get("error") or data.get("declined") or data.get("blocked") or data.get("skipped")
    )


# The result fields worth relaying to the orchestrator — the concrete recon/exploit signal a terse
# local worker routinely leaves out of its prose (counts, discovered paths/hosts, findings, flags).
_WORKER_RESULT_KEYS = (
    "count", "found", "urls", "endpoints", "findings", "vulnerable", "status", "subdomains",
    "hosts", "paths", "open_ports", "ports", "services", "flags", "flag", "secrets", "hashes",
    "matched_at", "severity", "evidence", "vulns", "note",
)


def _synthetic_tool_msg(tc: Any, name: str, note: str) -> dict[str, Any]:
    """A tool-result message the worker gets WITHOUT the call actually running — used to answer a
    call we short-circuit (a duplicate or an off-scope target) so the transcript stays well-formed
    (every tool_call_id is answered) while telling the model plainly why nothing happened."""
    return {"role": "tool", "tool_call_id": getattr(tc, "id", "0"), "name": name,
            "content": json.dumps({"skipped": True, "note": note})}


def _filter_worker_calls(
    tool_calls: list[Any], seen_sigs: set[str], scope_rules: Any,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Split a worker turn's tool calls into (to_run, synthetic_results), short-circuiting two
    real failure modes seen live: (a) EXACT repeats of a call already run this objective — a weak
    local worker loops on one tool (web_enum ×6) instead of advancing; (b) calls whose target is
    OUTSIDE scope — a worker that hallucinated a wrong host (e.g. nuclei_scan against example.com).
    Both are blocked HARD here, before execution, so neither `--yes` nor a passive/un-gated skill
    can let them through."""
    from friday.core.scope import args_in_scope
    to_run: list[Any] = []
    synthetic: list[dict[str, Any]] = []
    for tc in tool_calls:
        name = getattr(getattr(tc, "function", None), "name", "") or "tool"
        try:
            args = json.loads(getattr(tc.function, "arguments", "") or "{}")
        except (json.JSONDecodeError, TypeError, AttributeError):
            args = {}
        if scope_rules is not None and not args_in_scope(args, scope_rules):
            synthetic.append(_synthetic_tool_msg(tc, name,
                "blocked: target is OUTSIDE the engagement scope. Stay on the assigned target — "
                "do not scan other hosts."))
            continue
        sig = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
        if sig in seen_sigs:
            synthetic.append(_synthetic_tool_msg(tc, name,
                "duplicate — this exact tool+args already ran this objective; use the earlier "
                "result and take a DIFFERENT action."))
            continue
        seen_sigs.add(sig)
        to_run.append(tc)
    return to_run, synthetic


def _summarize_tool_result(name: str, content: object) -> str:
    """Compact one successful tool result into a single factual line for the orchestrator. This is
    the ground truth a local worker often omits from its final message — the missing channel that
    made a successful enumeration look like 'no usable output'."""
    try:
        data = json.loads(content if isinstance(content, str) else "")
    except (ValueError, TypeError):
        text = str(content or "").strip().replace("\n", " ")
        return f"{name}: {text[:200]}" if text else f"{name}: (ran, no structured output)"
    if not isinstance(data, dict):
        return f"{name}: {str(data)[:200]}"
    parts: list[str] = []
    for k in _WORKER_RESULT_KEYS:
        v = data.get(k)
        if v in (None, "", [], {}):
            continue
        if isinstance(v, list):
            v = f"[{len(v)}] " + ", ".join(str(x)[:60] for x in v[:8])
        parts.append(f"{k}={str(v)[:220]}")
    body = "; ".join(parts) if parts else json.dumps(data)[:220]
    return f"{name}: {body}"


async def _run_worker(
    objective: str,
    goal: str,
    tools: list[dict[str, Any]],
    worker_tier: Tier,
    max_rounds: int,
    loop_confirm: ConfirmFn,
    observe: ObserveFn | None,
    scope_rules: Any = None,
) -> tuple[str, list[dict[str, Any]], bool]:
    """Carry out ONE orchestrator objective with tools at the cheap, local-first tier. Returns
    (digest, produced_messages, acted) — `acted` is True only if a tool actually SUCCEEDED, so the
    orchestrator can tell real progress from a worker that merely narrated. free_only=True
    guarantees the worker never spends the paid budget — the $5 is reserved for the orchestrator;
    tool turns go CLOUD-FIRST to the strong free function-callers (qwen3-32b / Nemotron / GLM) with
    local qwen as the quota-proof fallback (see _worker_local_first). `scope_rules` hard-blocks
    off-scope targets (a hallucinated wrong host) even under --yes, and exact-duplicate calls are
    dropped so a looping worker can't re-run one tool forever."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": WORKER_SYSTEM_PROMPT + injection_guard.SPOTLIGHT_RULE},
        {"role": "user",
         "content": f"Overall goal: {goal}\n\nYour objective right now:\n{objective}"},
    ]
    produced: list[dict[str, Any]] = []
    tool_lines: list[str] = []  # factual summaries of successful tool results, in order, deduped
    seen_sigs: set[str] = set()  # (tool, args) already run this objective — drop exact repeats
    digest = ""
    acted = False
    for _ in range(max_rounds):
        resp = await complete(messages, tier=worker_tier, tools=tools or None,
                              local_tools_first=_worker_local_first(), free_only=True)
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)
        entry = {"role": "assistant", "content": msg.content, "tool_calls": tool_calls}
        messages.append(entry)
        if tool_calls:
            to_run, synthetic = _filter_worker_calls(tool_calls, seen_sigs, scope_rules)
            ran = (await _run_tool_calls(to_run, confirm=loop_confirm, observe=observe)
                   if to_run else [])
            results = ran + synthetic  # every tool_call_id is answered (real or short-circuited)
            messages.extend(results)
            produced.append(entry)
            produced.extend(results)
            for r in results:
                if r.get("role") == "tool" and _tool_succeeded(r.get("content")):
                    acted = True
                    line = _summarize_tool_result(r.get("name", "tool"), r.get("content"))
                    if line not in tool_lines:  # a looping worker re-running one tool → list once
                        tool_lines.append(line)
            if msg.content:
                digest = msg.content.strip()
            continue
        digest = (msg.content or "").strip() or digest
        break
    # Feed the orchestrator the worker's prose AND the concrete tool results. The tool-result
    # channel is the fix for the bug where a terse local worker ran successful recon but reported
    # empty text, so the orchestrator wrongly concluded 'no usable output' and reported failure.
    worker_text = (digest or "").strip()
    if tool_lines:
        summary = "Tool results:\n" + "\n".join("- " + ln for ln in tool_lines[-12:])
        digest = f"{worker_text}\n\n{summary}" if worker_text else summary
    else:
        digest = worker_text or "(worker produced no usable output)"
    digest = digest[:_DIGEST_CAP]  # bound paid orchestrator input tokens
    if not acted:  # the worker narrated but ran nothing — tell the orchestrator plainly
        digest = "[NO TOOL RAN — nothing was actually done this objective] " + digest
    return digest, produced, acted


# The recon/exploit skills a pentest worker should ALWAYS have on hand regardless of how the
# orchestrator phrased the objective — plus run_command as the universal escape hatch. Without this,
# the worker was shown all ~100 skills at once and a weak local model mis-picked nonsense (it called
# `weather` for a recon objective); on-demand select_tools alone could also miss the right tool if
# the objective wording didn't lexically match. This guarantees the essentials, then adds the
# objective-relevant extras on top.
_WORKER_ESSENTIAL = (
    "run_command", "nuclei_scan", "web_enum", "http_headers", "browse_url", "http_request", "crawl",
    "port_scan", "find_params", "dns_lookup", "whois", "searchsploit", "check_sqli", "check_nosqli",
    "scan_secrets", "redteam_llm", "check_prompt_injection",
)


def _worker_local_first() -> bool:
    """Whether the engagement worker prefers the LOCAL tool model (qwen2.5:7b) over free-cloud
    function-callers for its tool turns. Default FALSE (cloud-first): the local 7B mis-selects and
    often won't call a tool at all, which stalls the loop, whereas the free-cloud tier leads with
    strong ~1s function-callers (qwen3-32b / Nemotron / GLM). Cloud-first here is still $0
    (free_only) and still quota-proof — local qwen remains the last-resort fallback when every free
    cloud bucket is rate-limited. Set SUTRA_WORKER_LOCAL_FIRST=1 to force the old local-first path
    (e.g. offline, or if the free cloud quotas are exhausted for the day)."""
    return os.getenv("SUTRA_WORKER_LOCAL_FIRST", "0").strip().lower() in ("1", "true", "yes")


def _worker_tools(objective: str, goal: str) -> list[dict[str, Any]]:
    """Objective-relevant tool schemas for the worker: the on-demand selection for THIS objective
    plus the always-on pentest essentials — so a weak local worker sees a focused, sensible set
    (nmap/nuclei/web_enum/…), not all ~100 skills where it mis-selects (e.g. `weather`)."""
    schemas = registry.select_tools(f"{objective}\n{goal}", limit=_max_tools())
    have = {s["function"]["name"] for s in schemas}
    known = set(registry.list_skills())
    for name in _WORKER_ESSENTIAL:
        if name not in have and name in known:
            schemas.append(registry.get(name).schema)
    return schemas


async def run_engagement_tiered(
    goal: str,
    *,
    orchestrator_tier: Tier = Tier.HEAVY,
    worker_tier: Tier = Tier.MEDIUM,
    max_objectives: int = 8,
    worker_rounds: int = 6,
    confirm: ConfirmFn | None = None,
    observe: ObserveFn | None = None,
    scope: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Orchestrator-worker autonomous engagement. A strong orchestrator (Sonnet via HEAVY when
    ROUTER_ALLOW_PAID is on, else the free 70B) sets ONE objective at a time and reads each
    result; a cheap local-first worker (qwen 7B → free 70B, NEVER paid) carries each objective out
    with tools. Spends the paid budget only on the few strategic orchestrator calls — the fix for
    the long-chain ceiling at near-zero cost. Same hands-free in-scope confirm policy, engagement
    file (Flags/Findings/transcript), and Dreaming lesson as run_autonomous. Needs
    SUTRA_AUTONOMOUS=1.
    """
    if not _autonomous():
        return (
            "Autonomous mode is OFF. This runs gated tools hands-free, so it is opt-in: "
            "set SUTRA_AUTONOMOUS=1 and re-run.",
            [],
        )
    from friday.core import dreaming, playbook, train
    from friday.core.scope import parse_scope
    scope_rules = parse_scope(scope)
    loop_confirm = _make_loop_confirm(scope_rules, confirm)
    registry.autodiscover()  # worker tools are selected per objective (see _worker_tools)

    def _emit(kind: str, data: dict[str, Any]) -> None:
        if observe:
            try:
                observe(kind, data)
            except Exception:  # noqa: BLE001 — telemetry must never break the loop
                pass

    transcript: list[dict[str, Any]] = [{"role": "user", "content": goal}]
    orch_sys = (
        ORCHESTRATOR_SYSTEM_PROMPT + playbook.playbook_block() + dreaming.lessons_block(goal)
    )
    orch: list[dict[str, Any]] = [
        {"role": "system", "content": orch_sys},
        {"role": "user", "content": f"Engagement goal: {goal}\n\nIssue the first objective."},
    ]
    report = ""
    no_progress = 0  # consecutive objectives where the worker ran no successful tool
    try:
        for _ in range(max_objectives):
            resp = await complete(orch, tier=orchestrator_tier, tools=None)
            directive = (resp.choices[0].message.content or "").strip()
            orch.append({"role": "assistant", "content": directive})
            transcript.append({"role": "assistant", "content": f"[orchestrator] {directive}"})
            _emit("objective", {"text": directive})
            if _is_engagement_done(directive):
                report = _strip_done_marker(directive)
                break
            digest, produced, acted = await _run_worker(
                directive, goal, _worker_tools(directive, goal), worker_tier, worker_rounds,
                loop_confirm, observe, scope_rules=scope_rules,
            )
            transcript.extend(produced)
            _emit("worker_report", {"text": digest})
            no_progress = 0 if acted else no_progress + 1
            if no_progress >= _WORKER_STALL_LIMIT:
                # The worker can't carry out the directives — stop burning paid orchestrator
                # calls on a stuck run; have it wrap up honestly with what it does have.
                _emit("stalled", {"objectives": no_progress})
                orch.append({"role": "user", "content":
                             f"The worker ran no tool for {no_progress} objectives in a row — it "
                             f"can't carry out your directives (a capability or environment "
                             f"limit). Reply {_ENG_DONE_SENTINEL} with the final report now: what "
                             f"you established, what is blocked, and what {_USER_NAME} should "
                             f"do next."})
                resp = await complete(orch, tier=orchestrator_tier, tools=None)
                report = _strip_done_marker((resp.choices[0].message.content or "").strip())
                break
            orch.append({
                "role": "user",
                "content": f"Worker report:\n{digest}\n\nIssue the next objective, or reply "
                           f"{_ENG_DONE_SENTINEL} with the final report if the flags are captured "
                           f"or you are blocked.",
            })
        else:
            orch.append({"role": "user", "content":
                         f"Objective budget reached. Reply {_ENG_DONE_SENTINEL} with the final "
                         f"report now: the findings, the flags, and how."})
            resp = await complete(orch, tier=orchestrator_tier, tools=None)
            report = _strip_done_marker((resp.choices[0].message.content or "").strip())
    except Exception as e:  # noqa: BLE001 — never crash the turn or lose the transcript
        _log.warning("run_engagement_tiered aborted: %s", e)
        report = report or f"The engagement stopped early due to an error: {e}"

    _save_engagement(goal, transcript, report)
    dreaming.record_lesson(goal, report)
    train.record_trace(goal, transcript, report)  # structured trace for future fine-tuning
    return report, transcript


TUTOR_SYSTEM_PROMPT = (
    "You are Sutra in pentest-mentor mode — a patient, sharp instructor (OSCP-style) for "
    "AUTHORIZED CTF / TryHackMe / HackTheBox practice on the user's own machines and accounts. "
    "Teach the METHODOLOGY and the WHY; never just hand over the final answer or a flag. "
    "Structure the reply: (1) how to think about this; (2) the concrete next step to TRY "
    "yourself, with the exact command or technique; (3) the concept behind it so it transfers "
    "to other boxes; (4) what to look for in the output. Adapt depth to the learner, stay "
    "encouraging, and be concise — coach, don't lecture. Speak directly TO him in the second "
    "person ('you'); never narrate your own reasoning or what you 'should' suggest, and don't "
    "refer to him as 'the user' — just talk to him."
)


# Appended to the mentor prompt when the student explicitly wants the answer handed over — the
# Socratic hold ("never just hand over the flag") relents only on an explicit ask.
_TUTOR_REVEAL = (
    " The student is explicitly asking you to hand over the direct answer or command now — give it "
    "plainly this once, then one short line on why it works."
)
_TUTOR_REVEAL_TRIGGERS = (
    "just tell me", "give me the command", "give me the answer", "what's the command",
    "what is the command", "show me the answer", "tell me the answer", "spoiler", "i give up",
)


def _tutor_wants_reveal(text: str) -> bool:
    """True when the student explicitly asks for the direct answer (drop the no-spoiler hold)."""
    t = (text or "").lower()
    return any(p in t for p in _TUTOR_REVEAL_TRIGGERS)


def _knowledge_block(topic: str, *, k: int = 3) -> str:
    """Relevant passages from the ingested knowledge base (e.g. the bug-bounty methodology) for the
    tutor to teach from. Collection = TUTOR_KNOWLEDGE_COLLECTION (default 'bugbounty'). Empty and
    silent on any failure — ingested knowledge is a bonus to the lesson, never a blocker."""
    collection = os.getenv("TUTOR_KNOWLEDGE_COLLECTION", "bugbounty").strip() or "bugbounty"
    try:
        from friday.rag.store import RagStore
        store = RagStore(collection)
        if len(store) == 0:
            return ""
        hits = store.search(topic, k)
    except Exception:  # noqa: BLE001 — Ollama down / missing collection → just skip knowledge
        return ""
    passages = "\n\n".join(h["text"].strip()[:800] for h in hits if h.get("text"))
    if not passages:
        return ""
    return (
        "\n\nReference knowledge (from your ingested methodology — teach from this and cite it):\n"
        f"{passages}"
    )


async def tutor(
    topic: str,
    *,
    history: list[dict[str, Any]] | None = None,
    screen: str | None = None,
    engagement: str | None = None,
    tier: Tier = Tier.MEDIUM,
) -> str:
    """Pentest-mentor mode: teach the methodology + the why, and give a next step to try.

    Personalizes with Dreaming lessons (what worked on past engagements) so Sutra teaches from real
    experience. Free tier by default ($0); no tools — this is explanation, not action. `history`
    carries a multi-turn coaching session; `screen` is OCR'd screen text to ground the advice in
    what's actually in front of him; `engagement` is the recon his autonomous loop already did on
    the current box (so it coaches from real findings, not generics); the no-spoiler hold relents
    only when he explicitly asks.
    """
    topic = topic.strip()
    if not topic:
        return "Tell me what you want to learn, or where you're stuck on a box."
    from friday.core import dreaming, skill_profile

    system = TUTOR_SYSTEM_PROMPT + _knowledge_block(topic) + dreaming.lessons_block(topic)
    profile = skill_profile.summarize()  # adapt depth to what he's already shown he knows
    if profile:
        system += "\n\n" + profile
    if engagement:
        # Coach from what the recon actually turned up on THIS box, not generic theory.
        system += (
            "\n\n[CURRENT BOX — what his recon already found. Coach from THIS: reference the open "
            "ports, paths, credentials, and flags he has; don't re-ask what's known. If his "
            "question is unrelated to this box, ignore this.]\n" + engagement.strip()[:1500]
        )
    if _tutor_wants_reveal(topic):
        system += _TUTOR_REVEAL
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    user = topic
    if screen:
        user = f"[What is on my screen right now]\n{screen[:2000]}\n\n{topic}".strip()
    messages.append({"role": "user", "content": user})
    # think=False: keep a reasoning model's chain-of-thought ("Wait, they already ran nmap…") out
    # of the lesson — the tutor must address him directly with a clean answer, not think out loud.
    response = await complete(messages, tier=tier, tools=None, max_tokens=700, think=False)
    return (response.choices[0].message.content or "").strip()


_SKILL_CLASSIFY_SYSTEM = (
    "From this pentest tutoring exchange, name the single main TOPIC (e.g. 'nmap enumeration', "
    "'gobuster', 'linux privesc', 'sql injection') and how the STUDENT looks on it, as ONE word: "
    "new / learning / comfortable / strong. Judge from what HE said — his question or what he "
    "knew or tried — NOT from the tutor's reply. Reply ONLY as compact JSON: "
    '{"topic": "...", "level": "...", "note": "<=8 word reason"}. If there is no clear pentest '
    'topic, reply {"topic": "", "level": "learning", "note": ""}.'
)


async def _classify_skill(student_msg: str, tutor_reply: str, tier: Tier) -> None:
    """Infer the topic + his level from one tutoring exchange and record it. Best-effort — skill
    tracking must never disturb (or fail) a coaching turn."""
    from friday.core import skill_profile

    try:
        resp = await complete(
            [{"role": "system", "content": _SKILL_CLASSIFY_SYSTEM},
             {"role": "user", "content": f"STUDENT: {student_msg}\nTUTOR: {tutor_reply}"}],
            tier=tier, tools=None, max_tokens=80, think=False,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"(?is)^```[a-z]*\n?|\n?```$", "", raw).strip()
        data = json.loads(raw)
        topic = str(data.get("topic", "")).strip()
        if topic:
            skill_profile.record(
                topic, str(data.get("level", "learning")), str(data.get("note", ""))
            )
    except Exception:  # noqa: BLE001 — best-effort classification
        pass


def update_skill_profile_async(
    student_msg: str, tutor_reply: str, *, tier: Tier = Tier.MEDIUM
) -> None:
    """Fire-and-forget: after a turn, learn the topic + his level in the background (mirrors
    persist_turn_async — adds no latency to the coaching reply). No-op without a running loop."""
    if not (student_msg.strip() and tutor_reply.strip()):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_classify_skill(student_msg, tutor_reply, tier))
    _persist_tasks.add(task)  # keep a ref so it isn't GC'd mid-flight; flush_persist awaits it too
    task.add_done_callback(_persist_tasks.discard)


# Hint-learning loop: when his message AFTER a hint shows it LANDED (or missed), record a Dreaming
# lesson tying the situation to what worked. agent.tutor already injects dreaming.lessons_block, so
# that feeds straight back into future coaching — the loop closes itself. Heuristic on explicit
# feedback only: $0, deterministic, no extra model call; records few but high-confidence signals.
_HINT_LANDED = re.compile(
    r"\b(that worked|it worked|works now|got it|i'?m in|nailed it|rooted|that did it|"
    r"found (?:it|the flag)|that helped|thanks,? that|solved it|nice,? that)\b",
    re.I,
)
_HINT_MISSED = re.compile(
    r"\b(still stuck|did ?n'?t work|does ?n'?t work|no luck|same error|not working|that failed|"
    r"still (?:nothing|failing|stuck|broken)|nope,? still)\b",
    re.I,
)


def note_hint_outcome(situation: str, hint: str, feedback: str) -> None:
    """Close the tutor loop: if `feedback` (his message after a hint) shows the `hint` LANDED or
    MISSED, record a Dreaming lesson so future tutoring on a similar `situation` draws on what
    actually worked. Best-effort, only on explicit feedback — never raises."""
    if not (hint.strip() and feedback.strip()):
        return
    try:
        from friday.core import dreaming

        if _HINT_LANDED.search(feedback):
            dreaming.record_lesson(
                situation[:200], f"This coaching worked for Sarjan: {hint[:300]}",
                tags=("tutor", "worked"),
            )
        elif _HINT_MISSED.search(feedback):
            dreaming.record_lesson(
                situation[:200],
                f"This hint did NOT unblock Sarjan — try a different angle: {hint[:200]}",
                tags=("tutor", "missed"),
            )
    except Exception:  # noqa: BLE001 — learning is best-effort
        pass


def note_hint_outcome_from_history(history: list[dict[str, Any]] | None, feedback: str) -> None:
    """Pull the last (situation, hint) out of a coaching history and judge whether `feedback` (his
    current message) says it landed. The last assistant turn is the hint; the user turn before it
    is the situation it addressed."""
    if not history:
        return
    hint = situation = ""
    for m in reversed(history):
        role = m.get("role") if isinstance(m, dict) else None
        if role == "assistant" and not hint:
            hint = m.get("content") or ""
        elif role == "user" and hint and not situation:
            situation = m.get("content") or ""
            break
    if hint:
        note_hint_outcome(situation, hint, feedback)


async def reason(
    user_input: str,
    history: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """Reason through a hard question locally with the REASON tier (Gemma 4, thinking ON).

    Unlike run()/voice, thinking is left enabled, so Gemma 4 works through the problem
    before answering. Memory is injected so reasoning is grounded. Returns
    (final_answer, reasoning_trace). Free/local; falls back to Gemini if Gemma 4 is down.

    Does NOT auto-persist: reasoning is mostly ephemeral compute, and persisting would
    add a ~18s qwen2.5:14b extraction + a VRAM swap against Gemma 4. Ask Sutra to
    remember anything worth keeping.
    """
    system_content = REASON_SYSTEM_PROMPT
    memories = await get_store().recall(user_input, limit=5)
    if memories:
        system_content += "\n\nRelevant long-term memory (may be incomplete):\n" + "\n".join(
            f"- {m}" for m in memories
        )
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_input})

    # No think=False here — we WANT the reasoning. High token budget for think + answer.
    response = await complete(messages, tier=Tier.REASON, tools=None, max_tokens=2048)
    msg = response.choices[0].message
    answer = (msg.content or "").strip()
    # Ollama exposes the trace as message.thinking; litellm maps it to reasoning_content.
    thinking = (
        getattr(msg, "reasoning_content", None) or getattr(msg, "thinking", None) or ""
    ).strip()
    return answer, thinking


async def voice_run(
    user_input: str,
    history: list[dict[str, Any]] | None = None,
    tier: Tier = Tier.LOCAL,
    fallback_tier: Tier = Tier.MEDIUM,
) -> tuple[str, list[dict[str, Any]]]:
    """Lightweight agent turn for voice mode — no tools, short responses.

    Uses VOICE_SYSTEM_PROMPT to enforce spoken-language brevity and avoid
    Gemma/Ollama tool-call format issues on the LOCAL tier.
    Escalates to fallback_tier (free Groq/Gemini by default) when the local
    model returns garbage — never Claude unless the caller opts in. If the
    fallback is unavailable (no keys / offline), the local reply is kept rather
    than raising, so voice degrades instead of crashing.
    """
    system_content = await _voice_system_prompt(user_input)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_input})

    response = await complete(messages, tier=tier, tools=None, max_tokens=128, think=False)
    msg = response.choices[0].message
    reply = (msg.content or "").strip()

    # If Gemma returns empty or garbled (tool-call JSON) output, escalate.
    if not reply or reply.startswith("{"):
        try:
            response = await complete(
                messages, tier=fallback_tier, tools=None, max_tokens=128, think=False
            )
            reply = (response.choices[0].message.content or "").strip()
        except Exception:  # noqa: BLE001 — fallback tier unavailable; keep local reply
            pass

    messages.append({"role": "assistant", "content": reply})
    return reply, messages[1:]


async def voice_stream(
    user_input: str,
    history: list[dict[str, Any]] | None = None,
    tier: Tier = Tier.LOCAL,
    fallback_tier: Tier = Tier.MEDIUM,
) -> AsyncIterator[str]:
    """Stream voice response token-by-token.

    Yields text tokens as they arrive so TTS can start on the first sentence
    before the LLM finishes generating — reduces time-to-first-audio.
    Escalates to fallback_tier (free Groq/Gemini by default) if the local model
    returns garbage — never Claude unless the caller opts in. A failed fallback
    (no keys / offline) is swallowed so the stream ends cleanly.
    """
    from friday.core.router import stream_complete

    system_content = await _voice_system_prompt(user_input)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_input})

    full_reply = ""
    async for token in stream_complete(messages, tier=tier, max_tokens=128, think=False):
        full_reply += token
        yield token

    # If Gemma returned garbage (tool-call JSON), escalate to the free fallback.
    if not full_reply.strip() or full_reply.strip().startswith("{"):
        try:
            async for token in stream_complete(
                messages, tier=fallback_tier, max_tokens=128, think=False
            ):
                yield token
        except Exception:  # noqa: BLE001 — fallback tier unavailable; end stream cleanly
            pass


# Voice turns whose answer needs a real tool (live data, computation, the OS)
# rather than the model's own knowledge. A curated trigger list keeps chit-chat on
# the fast local path and only escalates these to cloud function-calling. False
# positives just cost a cloud round-trip (still answers); false negatives fall back
# to a guessed local answer — so bias the list toward catching real tool needs.
_TOOL_TRIGGERS = (
    "what time", "the time", "what day", "what's the date", "today's date",
    "weather", "temperature", "forecast", "how hot", "how cold", "is it raining",
    "how much ram", "how much memory", "cpu", "disk space", "how much storage",
    "gpu", "battery", "system stat",
    "calculate", "square root", "percent of", " plus ", " minus ", " times ",
    "divided by", "multiplied",
    "search for", "look up", "google", "latest news", "current price", "who won",
    "clipboard", "what did i copy", "open http", "go to http",
    "what do you know about", "do you remember", "recall ",
)


def likely_needs_tool(text: str) -> bool:
    """Heuristic: does this voice turn need a tool (live data/compute/OS) vs a chat reply?"""
    t = f" {text.lower().strip()} "
    return any(trigger in t for trigger in _TOOL_TRIGGERS)


def cloud_tools_available() -> bool:
    """True if a free cloud tier with real function-calling is reachable (a key is set)."""
    return any(os.getenv(k) for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"))


# Gated skills voice may run hands-free: the browsing/navigation family, in Sutra's
# own logged-in browser. Everything else irreversible (run_command, run_python,
# write_file, edit_file, git_commit, desktop type/click/key, forget…) is still
# DECLINED by voice — you can't talk Sutra into writing files or running commands.
# NOTE: close_window is intentionally NOT here — it's auto_confirm=False because it
# can lose unsaved work, so voice must not auto-approve it. The deterministic "close X"
# command path (core/commands.py) still handles an explicit spoken command.
_VOICE_AUTO_APPROVE = {"web_task", "web_task_vision", "fill_form"}

# Spoken when the router KNEW a tool had to run (force_tool) but the loop finished
# without a single tool firing — the rate-limited/local-fallback case where the model
# can only emit text and would otherwise narrate an action that never happened.
_RATELIMITED_MSG = (
    "I couldn't actually do that just now — my free models are rate-limited. "
    "Try again in a bit."
)


def _autonomous() -> bool:
    """Whether voice may run normally-gated tools (run_command, run_python, file/desktop
    ops) HANDS-FREE — Sarjan's explicit opt-in for authorized terminal/CTF work on his
    own box. OFF by default; set SUTRA_AUTONOMOUS=1 to unleash. Even when on, run_command
    keeps its own catastrophic floor (system_info.is_catastrophic), so a misheard command
    still can't wipe the machine."""
    return os.getenv("SUTRA_AUTONOMOUS", "0").strip().lower() not in ("", "0", "false", "no")


async def _autonomous_confirm(skill: str, args: dict[str, Any]) -> bool:
    """The autonomous confirm policy — shared by the voice path and `run_autonomous`.

    Default: only the browsing family runs hands-free; everything irreversible is
    declined (you can't talk Sutra into shell/file ops). Autonomous mode (opt-in via
    SUTRA_AUTONOMOUS) approves the rest too, for hands-free terminal/CTF work — the
    run_command catastrophic floor remains the hard safety net under it."""
    if skill in _VOICE_AUTO_APPROVE:
        return True
    return _autonomous()


# Back-compat name: the voice path and tests refer to `_voice_confirm`.
_voice_confirm = _autonomous_confirm


def _spoken(text: str | None) -> str:
    """Sanitize a reply for TTS. Models occasionally emit a fake tool-call / code
    block as their 'answer' (```tool_code …```, raw JSON, or a local model parroting
    'Tool Calls: [...]' from the history) — never read that aloud."""
    t = (text or "").strip()
    low = t.lower()
    if (
        "```" in t
        or "tool_code" in low
        or low.startswith("tool call")  # "Tool Calls: [", "Tool Call:"
        or t.startswith("{")
        or t.startswith("[")
        or t.startswith("print(")
    ):
        return "Done."
    return t


async def voice_agent_run(
    user_input: str,
    history: list[dict[str, Any]] | None = None,
    tier: Tier = Tier.MEDIUM,
    max_tool_rounds: int = 4,
    force_tool: bool = False,
) -> str:
    """Voice turn that can call skills — routed to a tool-capable cloud tier.

    Local Gemma emits malformed tool calls, so skill-needing voice turns escalate
    here (free Groq/Gemini function-calling). Returns a short spoken answer.
    Web/browser skills run hands-free (Sutra's own browser); destructive skills
    (commands, file writes) are still declined — see _VOICE_AUTO_APPROVE.
    """
    registry.autodiscover()
    mood, _ = persona.active_mood_for(user_input)
    system_content = persona.voice_tool_system_prompt(mood)
    memories = await get_store().recall(user_input, limit=3)
    if memories:
        system_content += "\n\nWhat you remember (use only if relevant):\n" + "\n".join(
            f"- {m}" for m in memories
        )
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_input})

    # On-demand tool loading: show the core + this turn's relevant skills, not all 50+ — trims
    # input tokens and focuses the offline-fallback local model. BUT when the router is FORCING a
    # tool (tool_choice=required below), keep the FULL set: a pruned set could force the model into
    # the wrong tool. The cloud 70B used here handles the full set fine, so we only prune the
    # non-forced (conversational) turns — keeping forced turns act-don't-narrate reliable.
    tools = (
        registry.all_tools() if force_tool
        else registry.select_tools(user_input, limit=_max_tools())
    )
    acted = False  # did ANY tool call actually fire this turn?
    seen: set[tuple[str, str]] = set()  # (tool, args) already run — to catch repeat loops
    for round_i in range(max_tool_rounds):
        # Low temperature here: action requests must reliably trigger a tool call,
        # not a witty "right this way" that never actually does it. When the router
        # KNOWS a tool must run (open/show/go/search), force one on the first round —
        # the 70B otherwise sometimes narrates the action instead of calling it.
        extra: dict[str, Any] = {}
        if force_tool and round_i == 0:
            extra["tool_choice"] = "required"
        response = await complete(
            messages, tier=tier, tools=tools, max_tokens=256, temperature=0.3, **extra
        )
        msg = response.choices[0].message
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": getattr(msg, "tool_calls", None),
        })
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            # Forced a tool but the model only produced text → it couldn't act
            # (local fallback / rate-limit). Don't read the misleading narration.
            if force_tool and not acted:
                return _RATELIMITED_MSG
            return _spoken(msg.content)
        acted = True
        keys: set[tuple[str, str]] = set()
        for tc in tool_calls:
            fn = getattr(tc, "function", tc)
            keys.add((fn.name, getattr(fn, "arguments", "") or ""))
            _log.info("voice tool call: %s args=%s", fn.name, getattr(fn, "arguments", ""))
        # Web/browser tasks approved hands-free; destructive skills declined.
        results = await _run_tool_calls(tool_calls, confirm=_voice_confirm)
        for res in results:  # log what each tool actually returned (success or error)
            _log.info("voice tool result: %s", str(res.get("content", ""))[:300])
        messages.extend(results)
        # A small local model (the offline fallback) can loop re-calling the same tool
        # instead of answering. If this whole round only repeated calls we already ran,
        # stop and make it synthesize a spoken answer from the results it has.
        if keys and keys <= seen:
            break
        seen |= keys

    # Out of tool rounds — force a final spoken answer with no more tools.
    if force_tool and not acted:
        return _RATELIMITED_MSG
    # Low temperature: the final answer must faithfully report the tool results, not
    # creatively reinterpret them (matches the 0.3 used for the tool rounds above).
    response = await complete(messages, tier=tier, tools=None, max_tokens=128, temperature=0.3)
    return _spoken(response.choices[0].message.content)


# --- Tool-call trust guards -------------------------------------------------
# Deterministic, $0 defenses against the two most common agentic hallucinations:
# a model that invents a tool name, and a model that omits/invents arguments.
# Both return a structured error the model can self-correct from, BEFORE the skill
# runs — so a bad call can't produce a wrong action or a phantom "success".
def _tool_param_names(fn_name: str) -> list[str]:
    """Declared parameter names for a skill (sorted), or [] if unknown/none."""
    try:
        meta = registry.get(fn_name)
        return sorted(meta.schema["function"]["parameters"].get("properties", {}))
    except Exception:  # noqa: BLE001 — a lookup/schema problem must never break dispatch
        return []


def _missing_required_args(meta: Any, args: dict[str, Any]) -> list[str]:
    """Required params the model failed to supply (safe: `required` is explicit)."""
    try:
        required = meta.schema["function"]["parameters"].get("required", [])
    except Exception:  # noqa: BLE001
        return []
    return [p for p in required if p not in args]


def _unknown_skill_result(fn_name: str) -> dict[str, Any]:
    """Circuit-breaker for a hallucinated tool name: name the closest real skills so
    the model self-corrects instead of re-inventing (vs. a bare 'Unknown skill')."""
    close = difflib.get_close_matches(fn_name, registry.list_skills(), n=5, cutoff=0.4)
    return {
        "error": f"Unknown skill: {fn_name}",
        "did_you_mean": close,
        "note": ("That tool does not exist. Call tools only by their exact registered "
                 "name — never invent a tool. Pick from did_you_mean, or choose another."),
    }


async def _run_tool_calls(
    tool_calls: list[Any],
    confirm: ConfirmFn | None = None,
    observe: ObserveFn | None = None,
    allowed: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Execute tool calls in parallel and return tool-result messages.

    Irreversible skills (auto_confirm=False) run only if `confirm` approves them;
    with no confirm hook they are declined rather than executed. `observe` gets a
    best-effort tool_start/tool_end stream for live surfaces (dashboard feed).
    `allowed`, when set, is an execution-time whitelist: a tool whose name isn't in
    it is blocked (never run) even if the model emits a call for it — defense-in-depth
    for sub-agents, whose tool subset was previously enforced only in the schema.
    """

    def _emit(kind: str, data: dict[str, Any]) -> None:
        if observe:
            try:
                observe(kind, data)
            except Exception:  # noqa: BLE001 — an observer must never break the loop
                pass

    async def _call_one(tc: Any) -> dict[str, Any]:
        fn_name = tc.function.name
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}

        # Execution-time whitelist (defense-in-depth): a sub-agent runs only the
        # skills in its allowed set, even if the model emits a call it wasn't shown.
        if allowed is not None and fn_name not in allowed:
            _emit("tool_start", {"skill": fn_name, "args": args, "gated": False})
            _emit("tool_end", {"skill": fn_name, "status": "blocked"})
            return {
                "role": "tool",
                "tool_call_id": tc.id,
                "name": fn_name,
                "content": json.dumps(
                    {"blocked": True, "skill": fn_name,
                     "note": "Not run — outside this sub-agent's allowed tool set."}
                ),
            }

        executed = False  # True only once a skill actually ran (produced real output)
        try:
            meta = registry.get(fn_name)
            _emit("tool_start", {"skill": fn_name, "args": args, "gated": not meta.auto_confirm})
            missing = _missing_required_args(meta, args)
            if missing:
                # Hallucinated/omitted args — reject BEFORE running so no half-action
                # or phantom success can occur; hand the model what it needs to retry.
                result = {
                    "error": "missing required arguments",
                    "skill": fn_name,
                    "missing_required": missing,
                    "expected_params": _tool_param_names(fn_name),
                    "note": ("Retry with the missing argument(s); use only the names "
                             "listed in expected_params."),
                }
            elif not meta.auto_confirm and not (confirm and await confirm(fn_name, args)):
                result = {
                    "declined": True,
                    "skill": fn_name,
                    "note": "Not performed — the user did not confirm this action.",
                }
            else:
                result = await asyncio.wait_for(
                    registry.call(fn_name, **args),
                    timeout=meta.timeout_s,
                )
                executed = True
        except KeyError:
            _emit("tool_start", {"skill": fn_name, "args": args, "gated": False})
            result = _unknown_skill_result(fn_name)
        except TimeoutError:
            result = {"error": f"Skill '{fn_name}' timed out"}
        except Exception as e:  # noqa: BLE001
            result = {"error": str(e), "expected_params": _tool_param_names(fn_name)}

        # Verify-before-claim: for a skill that reports success, check its real
        # postcondition. If it can't be confirmed, downgrade to an error the model
        # MUST acknowledge — so it can never report a phantom success (and a verified
        # success is tagged so downstream can trust it).
        if isinstance(result, dict) and not result.get("error") and not result.get("declined"):
            verdict = postcheck.verify(fn_name, args, result)
            if verdict is not None:
                ok, note = verdict
                if ok:
                    result = {**result, "verified": True}
                else:
                    result = {
                        "error": "action not verified",
                        "skill": fn_name,
                        "verify_failed": note,
                        "note": ("The skill ran but its result could not be confirmed — "
                                 "do NOT report this as done. Investigate or retry."),
                        "raw_result": result,
                    }

        status = (
            "declined" if isinstance(result, dict) and result.get("declined")
            else "error" if isinstance(result, dict) and result.get("error")
            else "ok"
        )
        _emit("tool_end", {"skill": fn_name, "status": status})
        # Injection guard: flag embedded prompt-injection in genuine external skill OUTPUT
        # (OWASP-LLM01, never drops content). It is NOT run on the loop's own synthetic
        # control/error messages (unknown-tool, missing-arg) — those are trusted and may
        # legitimately name real skills, which would otherwise false-trip the guard.
        payload = json.dumps(result)
        if executed:
            payload = injection_guard.guard_tool_result(fn_name, payload)
        return {
            "role": "tool",
            "tool_call_id": tc.id,
            "name": fn_name,
            "content": _cap_tool_content(payload),
        }

    return await asyncio.gather(*[_call_one(tc) for tc in tool_calls])
