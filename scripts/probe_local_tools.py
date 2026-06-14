"""Probe: can a LOCAL ollama model emit real tool_calls with the full tool schema?

This is the linchpin test for making tool-calling quota-proof. If qwen2.5 picks
the right tool locally, we can fall back to it when every cloud model is rate-
limited — and Sutra acts offline instead of narrating. Measures, does not assume.

Run: .venv/bin/python scripts/probe_local_tools.py
"""

from __future__ import annotations

import asyncio
import json
import sys

import litellm

from friday.skills import registry

litellm.drop_params = True

MODELS = [
    "ollama_chat/qwen2.5:7b",
    "ollama_chat/qwen2.5:14b",
    "ollama_chat/llama3.2:3b",
    "ollama_chat/gemma3:4b",  # the current fallback — expected to fail
]

# Realistic action requests the deterministic router might miss / that need an LLM.
PROMPTS = [
    ("open firefox", "open_app"),
    ("what time is it", "current_time"),
    ("how much ram do i have", None),  # any system/info tool is fine
    ("search the web for the latest kali linux version", "web_search"),
]

SYSTEM = (
    "You are Sutra, a voice assistant. Use the provided tools to actually perform "
    "actions and get real data instead of guessing. Call the matching tool."
)


async def probe_one(model: str, prompt: str, tools: list[dict]) -> dict:
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ]
    try:
        resp = await litellm.acompletion(
            model=model,
            messages=messages,
            tools=tools,
            temperature=0.2,
            max_tokens=256,
            timeout=90,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    msg = resp.choices[0].message
    tcs = getattr(msg, "tool_calls", None) or []
    if not tcs:
        return {"ok": False, "called": None, "content": (msg.content or "")[:120]}
    fn = tcs[0].function
    try:
        args = json.loads(fn.arguments or "{}")
    except json.JSONDecodeError:
        args = {"_raw": fn.arguments}
    return {"ok": True, "called": fn.name, "args": args}


async def main() -> None:
    registry.autodiscover()
    tools = registry.all_tools()
    print(f"Tool schema count: {len(tools)}\n")

    for model in MODELS:
        print(f"=== {model} ===")
        for prompt, expected in PROMPTS:
            res = await probe_one(model, prompt, tools)
            if res["ok"]:
                hit = "✓" if (expected is None or res["called"] == expected) else "≈"
                print(f"  {hit} {prompt!r:55} -> {res['called']}({res['args']})")
            else:
                err = res.get("error") or f"NO TOOL CALL (said: {res.get('content','')!r})"
                print(f"  ✗ {prompt!r:55} -> {err}")
        print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
