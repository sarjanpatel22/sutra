"""Head-to-head benchmark for Sutra's free cloud-brain candidates.

Answers "should we swap the MEDIUM-tier model?" with DATA, not vibes — the lesson
that saved the pointless qwen3-embedding and qwen3-32b swaps. For each model it
measures the two things that actually decide Sutra's cloud brain:

  • tool-calling: given the REAL skill registry, does it emit a correct tool_call
    (and does it even ACCEPT the full tools array, or reject it as "request too large")?
  • output hygiene + latency: does it leak <think> reasoning tokens into content
    (bad for terse/voice replies), and how fast is a turn?

Free models only ($0). Run it again whenever the Groq/Gemini catalog changes:
    .venv/bin/python scripts/bench_models.py
Override the list with BENCH_MODELS="groq/...,gemini/..." (comma-separated).
"""

from __future__ import annotations

import asyncio
import os
import re
import time

_THINK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL)

_DEFAULT_MODELS = (
    "groq/llama-3.3-70b-versatile",
    "groq/qwen/qwen3-32b",
    "groq/openai/gpt-oss-120b",
    "gemini/gemini-2.5-flash",
)
_TOOL_PROMPTS = (
    "What time is it right now?",
    "How much RAM does this computer have?",
    "Search the web for the latest AI news.",
)
_CHAT_PROMPTS = (
    "In two sentences, explain the difference between TCP and UDP.",
    "Write a Python function fib(n) returning the nth Fibonacci number iteratively.",
)
_SYS_TOOL = (
    "You are Sutra. When the user asks you to fetch or do something real, CALL the "
    "matching tool. Do not guess."
)


def _classify_error(err: str) -> str:
    low = err.lower()
    if "too large" in low:
        return "REQUEST-TOO-LARGE (can't fit Sutra's tools)"
    if "rate limit" in low or "ratelimit" in low:
        return "rate-limited (free-tier throttle)"
    return err[:80]


async def _call(model: str, prompt: str, tools: list | None) -> dict:
    import litellm

    t0 = time.monotonic()
    sys_content = _SYS_TOOL if tools else "You are concise and helpful."
    try:
        kw: dict = dict(
            model=model,
            messages=[
                {"role": "system", "content": sys_content},
                {"role": "user", "content": prompt},
            ],
            max_tokens=450,
            temperature=0.2,
        )
        if tools:
            kw["tools"] = tools
        r = await litellm.acompletion(**kw)
        ms = round((time.monotonic() - t0) * 1000)
        msg = r.choices[0].message
        tcs = getattr(msg, "tool_calls", None) or []
        content = msg.content or ""
        return {
            "ms": ms,
            "tools": [tc.function.name for tc in tcs],
            "thinks": bool(_THINK.search(content)),
            "text": _THINK.sub("", content).strip().replace("\n", " ")[:150],
            "err": None,
        }
    except Exception as e:  # noqa: BLE001 — record the failure, keep benchmarking
        return {"ms": round((time.monotonic() - t0) * 1000), "tools": [], "thinks": False,
                "text": "", "err": _classify_error(str(e))}


async def main() -> None:
    from friday.core.secrets import load_into_env
    from friday.skills import registry

    load_into_env()
    if not os.getenv("GROQ_API_KEY") and not os.getenv("GEMINI_API_KEY"):
        print("No GROQ_API_KEY / GEMINI_API_KEY in env — nothing to benchmark.")
        return
    registry.autodiscover()
    tools = registry.all_tools()
    raw = os.getenv("BENCH_MODELS", ",".join(_DEFAULT_MODELS))
    models = [m.strip() for m in raw.split(",") if m.strip()]

    for model in models:
        print(f"\n============ {model} ============")
        for p in _TOOL_PROMPTS:
            r = await _call(model, p, tools)
            note = f"ERR={r['err']}" if r["err"] else f"fired={r['tools'] or 'NONE'}"
            print(f"[TOOL] {p[:38]:40} {r['ms']:>6}ms  {note}")
            await asyncio.sleep(1.0)  # be gentle on the free tier
        for p in _CHAT_PROMPTS:
            r = await _call(model, p, None)
            flags = " THINK-LEAK" if r["thinks"] else ""
            note = f"ERR={r['err']}" if r["err"] else f"{r['ms']}ms{flags}"
            print(f"[CHAT] {p[:38]:40} {note}")
            if r["text"]:
                print(f"        -> {r['text']}")
            await asyncio.sleep(1.0)


if __name__ == "__main__":
    asyncio.run(main())
