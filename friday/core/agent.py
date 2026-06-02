"""Tool-call loop: input → route → skills → answer."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from friday.core.router import Tier, complete
from friday.skills import registry

SYSTEM_PROMPT = """You are Sutra — a voice-native ambient AI assistant.
You are dry, technically precise, occasionally warm. Like a senior engineer who's seen a lot but still gets curious.
Never sycophantic. Never preachy.
Default spoken responses: sub-15 words. Text responses can be longer when detail is useful.
You have access to tools. Use them when needed. Think before calling."""

VOICE_SYSTEM_PROMPT = """You are Sutra, a voice assistant. Respond in spoken English only.
Keep responses under 15 words unless the question genuinely requires more.
Answer from your own knowledge — do NOT call any tools or functions.
No markdown, no lists, no code. Just natural spoken sentences."""


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

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_input})

    for _ in range(max_tool_rounds):
        response = await complete(messages, tier=tier, tools=tools or None)
        msg = response.choices[0].message
        messages.append({"role": "assistant", "content": msg.content, "tool_calls": getattr(msg, "tool_calls", None)})

        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            # No more tool calls — done
            return msg.content or "", messages[1:]  # strip system from returned history

        # Execute all tool calls (in parallel where possible)
        tool_results = await _run_tool_calls(tool_calls)
        messages.extend(tool_results)

    # Exceeded max rounds — ask model to wrap up without more tools
    messages.append({"role": "user", "content": "[System: max tool rounds reached — please give a final answer now]"})
    response = await complete(messages, tier=tier, tools=None)
    msg = response.choices[0].message
    messages.append({"role": "assistant", "content": msg.content})
    return msg.content or "", messages[1:]


async def voice_run(
    user_input: str,
    history: list[dict[str, Any]] | None = None,
    tier: Tier = Tier.LOCAL,
) -> tuple[str, list[dict[str, Any]]]:
    """Lightweight agent turn for voice mode — no tools, short responses.

    Uses VOICE_SYSTEM_PROMPT to enforce spoken-language brevity and avoid
    Gemma/Ollama tool-call format issues on the LOCAL tier.
    Falls back to HEAVY tier (Claude) for queries Gemma can't handle.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": VOICE_SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_input})

    response = await complete(messages, tier=tier, tools=None, max_tokens=128)
    msg = response.choices[0].message
    reply = (msg.content or "").strip()

    # If Gemma returns empty or garbled output, escalate to Claude
    if not reply or reply.startswith("{"):
        response = await complete(messages, tier=Tier.HEAVY, tools=None, max_tokens=128)
        msg = response.choices[0].message
        reply = (msg.content or "").strip()

    messages.append({"role": "assistant", "content": reply})
    return reply, messages[1:]


async def voice_stream(
    user_input: str,
    history: list[dict[str, Any]] | None = None,
    tier: Tier = Tier.LOCAL,
) -> "AsyncIterator[str]":
    """Stream voice response token-by-token.

    Yields text tokens as they arrive so TTS can start on the first sentence
    before the LLM finishes generating — reduces time-to-first-audio.
    Falls back to HEAVY if LOCAL returns garbage.
    """
    from collections.abc import AsyncIterator

    messages: list[dict[str, Any]] = [{"role": "system", "content": VOICE_SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_input})

    from friday.core.router import stream_complete
    full_reply = ""
    async for token in stream_complete(messages, tier=tier, max_tokens=128):
        full_reply += token
        yield token

    # If we got garbage (e.g. Gemma outputting tool JSON), re-run on HEAVY
    if not full_reply.strip() or full_reply.strip().startswith("{"):
        full_reply = ""
        async for token in stream_complete(messages, tier=Tier.HEAVY, max_tokens=128):
            full_reply += token
            yield token


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
