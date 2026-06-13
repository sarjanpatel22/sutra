"""Tool-call loop: input → route → skills → answer."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from friday.core import persona
from friday.core.router import Tier, complete
from friday.core.turnlog import log_text_turn
from friday.memory.store import get_store, looks_like_noise
from friday.skills import registry

_log = logging.getLogger("sutra.agent")

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
You have access to tools. Use them when needed. Think before calling."""

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
) -> tuple[str, list[dict[str, Any]]]:
    """Run one turn of the agent loop.

    confirm: async (skill_name, args) -> bool, consulted before any
    auto_confirm=False (irreversible) skill runs. Without it those skills are
    declined — never silently executed — so the confirmation gate can't be
    bypassed, but with it they actually run after approval.

    Returns (assistant_reply, updated_history).
    """
    registry.autodiscover()
    # Local Ollama models (gemma) emit tool calls as malformed text rather than
    # proper tool_calls — it leaked raw JSON as the answer. Only the cloud tiers,
    # which do real function-calling, get tools.
    tools = None if tier in (Tier.LOCAL, Tier.REASON) else registry.all_tools()

    # Pull relevant long-term memory into context (no-op if memory unavailable).
    system_content = SYSTEM_PROMPT
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
    stats = {"rounds": 0, "model": "", "in_tokens": 0, "out_tokens": 0}

    def _account(response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage:
            stats["in_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
            stats["out_tokens"] += getattr(usage, "completion_tokens", 0) or 0
        stats["model"] = getattr(response, "model", None) or stats["model"]

    def _finish(reply: str) -> tuple[str, list[dict[str, Any]]]:
        log_text_turn(
            tier=tier.value,
            model=stats["model"],
            tool_rounds=stats["rounds"],
            total_ms=(time.monotonic() - t_start) * 1000,
            in_tokens=stats["in_tokens"],
            out_tokens=stats["out_tokens"],
            preview=user_input,
        )
        # Persist in the background so the reply returns now (was a ~18s block).
        persist_turn_async(user_input, reply, source="conversation")
        return reply, messages[1:]  # strip system from returned history

    for _ in range(max_tool_rounds):
        response = await complete(messages, tier=tier, tools=tools or None)
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

        stats["rounds"] += 1
        # Execute all tool calls (in parallel where possible)
        tool_results = await _run_tool_calls(tool_calls, confirm=confirm, observe=observe)
        messages.extend(tool_results)

    # Exceeded max rounds — ask model to wrap up without more tools
    messages.append(
        {"role": "user",
         "content": "[System: max tool rounds reached — please give a final answer now]"}
    )
    response = await complete(messages, tier=tier, tools=None)
    _account(response)
    msg = response.choices[0].message
    messages.append({"role": "assistant", "content": msg.content})
    return _finish(msg.content or "")


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
_VOICE_AUTO_APPROVE = {"web_task", "web_task_vision", "fill_form", "close_window"}


async def _voice_confirm(skill: str, args: dict[str, Any]) -> bool:
    """Voice's confirmation policy: approve web/browser tasks, decline destructive ops."""
    return skill in _VOICE_AUTO_APPROVE


def _spoken(text: str | None) -> str:
    """Sanitize a reply for TTS. The 70B occasionally emits a fake tool-call / code
    block as its 'answer' (```tool_code …```, raw JSON) — never read that aloud."""
    t = (text or "").strip()
    if "```" in t or "tool_code" in t or t.startswith("{") or t.startswith("print("):
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

    tools = registry.all_tools()
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
            return _spoken(msg.content)
        names = ", ".join(getattr(tc, "function", tc).name for tc in tool_calls)
        _log.info("voice tool call(s): %s", names)
        # Web/browser tasks approved hands-free; destructive skills declined.
        messages.extend(await _run_tool_calls(tool_calls, confirm=_voice_confirm))

    # Out of tool rounds — force a final spoken answer with no more tools.
    response = await complete(messages, tier=tier, tools=None, max_tokens=128)
    return _spoken(response.choices[0].message.content)


async def _run_tool_calls(
    tool_calls: list[Any],
    confirm: ConfirmFn | None = None,
    observe: ObserveFn | None = None,
) -> list[dict[str, Any]]:
    """Execute tool calls in parallel and return tool-result messages.

    Irreversible skills (auto_confirm=False) run only if `confirm` approves them;
    with no confirm hook they are declined rather than executed. `observe` gets a
    best-effort tool_start/tool_end stream for live surfaces (dashboard feed).
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

        try:
            meta = registry.get(fn_name)
            _emit("tool_start", {"skill": fn_name, "args": args, "gated": not meta.auto_confirm})
            if not meta.auto_confirm and not (confirm and await confirm(fn_name, args)):
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
        except KeyError:
            _emit("tool_start", {"skill": fn_name, "args": args, "gated": False})
            result = {"error": f"Unknown skill: {fn_name}"}
        except TimeoutError:
            result = {"error": f"Skill '{fn_name}' timed out"}
        except Exception as e:  # noqa: BLE001
            result = {"error": str(e)}

        status = (
            "declined" if isinstance(result, dict) and result.get("declined")
            else "error" if isinstance(result, dict) and result.get("error")
            else "ok"
        )
        _emit("tool_end", {"skill": fn_name, "status": status})
        return {
            "role": "tool",
            "tool_call_id": tc.id,
            "name": fn_name,
            "content": json.dumps(result),
        }

    return await asyncio.gather(*[_call_one(tc) for tc in tool_calls])
