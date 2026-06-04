"""Tool-call loop: input → route → skills → answer."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

from friday.core.router import Tier, complete
from friday.memory.store import get_store
from friday.skills import registry

# Who Sutra is talking to. Recalled memory stores facts as "<USER_NAME> is …", so
# the prompt must tell the model that name == the person it's speaking with,
# otherwise "what's my name?" recalls "Sarjan is from Sudbury" but answers "user".
_USER_NAME = os.getenv("USER_NAME", "Sarjan")

SYSTEM_PROMPT = f"""You are Sutra — a voice-native ambient AI assistant talking with {_USER_NAME}.
You are dry, technically precise, occasionally warm. Like a senior engineer who's seen a lot but still gets curious.
Never sycophantic. Never preachy.
Recalled facts about {_USER_NAME} describe the person you are speaking with — treat "{_USER_NAME}" and "you/your" as the same person.
Default spoken responses: sub-15 words. Text responses can be longer when detail is useful.
You have access to tools. Use them when needed. Think before calling."""

VOICE_SYSTEM_PROMPT = f"""You are Sutra, a voice assistant talking with {_USER_NAME}.
Recalled facts about {_USER_NAME} describe the person you are speaking with — treat "{_USER_NAME}" and "you/your" as the same person.
Respond in spoken English only.
Keep responses under 15 words unless the question genuinely requires more.
Answer from your own knowledge — do NOT call any tools or functions.
No markdown, no lists, no code. Just natural spoken sentences."""

# Reasoning mode (`sutra think`): no voice-brevity rule — think it through and
# answer in full. Same persona + identity anchor.
REASON_SYSTEM_PROMPT = f"""You are Sutra — reasoning carefully through a hard problem for {_USER_NAME}.
You are dry, technically precise, occasionally warm — a senior engineer who reasons from first principles and isn't afraid to say "it depends, and here's why".
Recalled facts about {_USER_NAME} describe the person you are speaking with — treat "{_USER_NAME}" and "you/your" as the same person; use them when relevant.
Work the problem through, then give a complete, decisive answer — as long as it genuinely needs, no longer. State your conclusion and the key reasoning behind it. No artificial word limit; never truncate the actual answer. For puzzles, commit to a solution rather than asking for more constraints."""


# Background voice-turn persistence. Voice can't await remember() (it runs the
# ~18s extraction LLM), so turns are persisted fire-and-forget. The lock serializes
# them so only one qwen2.5:14b extraction runs at a time — concurrent loads would
# thrash gemma3:4b out of VRAM and slow the next voice turn.
_persist_lock = asyncio.Lock()
_persist_tasks: set[asyncio.Task[None]] = set()


def persist_turn_async(user_input: str, reply: str) -> None:
    """Schedule a voice turn to be saved to memory without blocking the loop."""
    if not reply.strip():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no event loop (e.g. sync caller/tests) — skip silently

    async def _persist() -> None:
        async with _persist_lock:  # one extraction at a time
            try:
                await get_store().remember(
                    f"User: {user_input}\nSutra: {reply}", source="voice"
                )
            except Exception:  # noqa: BLE001 — persistence is best-effort
                pass

    task = loop.create_task(_persist())
    _persist_tasks.add(task)  # keep a ref so it isn't GC'd mid-flight
    task.add_done_callback(_persist_tasks.discard)


async def _voice_system_prompt(user_input: str) -> str:
    """VOICE_SYSTEM_PROMPT plus any relevant recalled memory.

    recall() is fast (~40ms warm; keyword fallback ~2ms) and never raises, so this
    stays within the voice latency budget. Voice only READS memory here — it does
    not persist turns (that runs the ~18s extraction LLM; the ambient daemon and
    explicit remember handle writes).
    """
    facts = await get_store().recall(user_input, limit=3)
    if not facts:
        return VOICE_SYSTEM_PROMPT
    return VOICE_SYSTEM_PROMPT + "\n\nWhat you remember (use only if relevant):\n" + "\n".join(
        f"- {f}" for f in facts
    )


async def run(
    user_input: str,
    history: list[dict[str, Any]] | None = None,
    tier: Tier = Tier.HEAVY,
    max_tool_rounds: int = 5,
) -> tuple[str, list[dict[str, Any]]]:
    """Run one turn of the agent loop.

    Returns (assistant_reply, updated_history).
    """
    registry.autodiscover()
    tools = registry.all_tools()

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

    async def _finish(reply: str) -> tuple[str, list[dict[str, Any]]]:
        # Persist the turn (no-op/instant if memory is unavailable).
        await get_store().remember(f"User: {user_input}\nSutra: {reply}", source="conversation")
        return reply, messages[1:]  # strip system from returned history

    for _ in range(max_tool_rounds):
        response = await complete(messages, tier=tier, tools=tools or None)
        msg = response.choices[0].message
        messages.append({"role": "assistant", "content": msg.content, "tool_calls": getattr(msg, "tool_calls", None)})

        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            # No more tool calls — done
            return await _finish(msg.content or "")

        # Execute all tool calls (in parallel where possible)
        tool_results = await _run_tool_calls(tool_calls)
        messages.extend(tool_results)

    # Exceeded max rounds — ask model to wrap up without more tools
    messages.append({"role": "user", "content": "[System: max tool rounds reached — please give a final answer now]"})
    response = await complete(messages, tier=tier, tools=None)
    msg = response.choices[0].message
    messages.append({"role": "assistant", "content": msg.content})
    return await _finish(msg.content or "")


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
    thinking = (getattr(msg, "reasoning_content", None) or getattr(msg, "thinking", None) or "").strip()
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
            response = await complete(messages, tier=fallback_tier, tools=None, max_tokens=128, think=False)
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
            async for token in stream_complete(messages, tier=fallback_tier, max_tokens=128, think=False):
                yield token
        except Exception:  # noqa: BLE001 — fallback tier unavailable; end stream cleanly
            pass


async def _run_tool_calls(tool_calls: list[Any]) -> list[dict[str, Any]]:
    """Execute tool calls in parallel and return tool-result messages."""

    async def _call_one(tc: Any) -> dict[str, Any]:
        fn_name = tc.function.name
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}

        try:
            meta = registry.get(fn_name)
            if not meta.auto_confirm:
                # In text mode, surface confirmation request
                result = {"confirmation_required": True, "skill": fn_name, "args": args}
            else:
                result = await asyncio.wait_for(
                    registry.call(fn_name, **args),
                    timeout=meta.timeout_s,
                )
        except KeyError:
            result = {"error": f"Unknown skill: {fn_name}"}
        except asyncio.TimeoutError:
            result = {"error": f"Skill '{fn_name}' timed out"}
        except Exception as e:  # noqa: BLE001
            result = {"error": str(e)}

        return {
            "role": "tool",
            "tool_call_id": tc.id,
            "name": fn_name,
            "content": json.dumps(result),
        }

    return await asyncio.gather(*[_call_one(tc) for tc in tool_calls])
