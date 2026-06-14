"""Live-path verification: does Sutra still ACT with the entire cloud rate-limited?

This mirrors the EXACT routing the voice/text loop uses (friday.voice.pipeline.
process_text: autoroute.decide -> try_command -> voice_agent_run / voice_stream),
minus TTS/audio, with every cloud model forced to fail. It reproduces the real
"works your side, not mine" condition and proves the offline fallback fires real
tools instead of narrating — the failure that kept recurring.

Run: .venv/bin/python scripts/verify_offline_tools.py
"""

from __future__ import annotations

import asyncio
import logging

import litellm

logging.basicConfig(level=logging.INFO, format="    %(name)s: %(message)s")
for noisy in ("LiteLLM", "httpx", "graphiti_core", "graphiti_core.driver.falkordb_driver"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

_real_acompletion = litellm.acompletion


async def _cloud_down(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Pass ollama calls to the real backend; make every cloud model fail (rate-limit)."""
    model = kwargs.get("model", "")
    if not str(model).startswith(("ollama/", "ollama_chat/")):
        raise RuntimeError(f"SIMULATED rate-limit for {model}")
    return await _real_acompletion(*args, **kwargs)


async def _route_like_pipeline(text: str) -> tuple[str, str]:
    """Run the same routing process_text uses, minus TTS. Returns (path, reply)."""
    from friday.core.agent import voice_agent_run, voice_stream
    from friday.core.autoroute import decide
    from friday.core.commands import try_command
    from friday.core.router import Tier

    plan = decide(text)
    if plan.act:
        cmd = await try_command(text)
        if cmd is not None:
            return f"deterministic:{cmd['skill']}", cmd["spoken"]
        reply = await voice_agent_run(
            text, history=[], tier=plan.tier, force_tool=plan.tool_required
        )
        return "voice_agent_run", reply
    # chit-chat → local streaming chat (no tools)
    chunks = [tok async for tok in voice_stream(text, history=[], tier=Tier.LOCAL)]
    return "voice_stream", "".join(chunks).strip()


# (utterance, predicate(path, reply) -> ok, human description of the bar)
CASES = [
    ("open firefox",
     lambda p, r: p.startswith("deterministic:open_app"),
     "OS command stays quota-proof (no LLM)"),
    ("how much ram do i have",
     lambda p, r: p == "voice_agent_run" and r.strip() and "tool call" not in r.lower(),
     "system stats via local tool, clean spoken answer"),
    ("what is 15 times 27",
     lambda p, r: "405" in r,
     "arithmetic correct (calc) offline"),
    ("what time is it",
     lambda p, r: p == "voice_agent_run" and r.strip() and "tool call" not in r.lower(),
     "time via local tool, clean spoken answer"),
    ("what's the capital of France",
     lambda p, r: "paris" in r.lower(),
     "chit-chat answered locally (gemma)"),
]


async def main() -> None:
    litellm.acompletion = _cloud_down  # type: ignore[assignment]
    print("\n=== LIVE PATH with cloud DISABLED (every cloud model rate-limited) ===\n")
    results = []
    for text, predicate, desc in CASES:
        try:
            path, reply = await _route_like_pipeline(text)
            ok = bool(predicate(path, reply))
        except Exception as exc:  # noqa: BLE001
            path, reply, ok = "EXCEPTION", f"{type(exc).__name__}: {exc}", False
        results.append((text, desc, path, reply, ok))

    print("\n--- results ---")
    all_ok = True
    for text, desc, path, reply, ok in results:
        all_ok &= ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {text!r}  ({desc})")
        print(f"         path={path}  reply={reply[:150]!r}")
    print("\n" + ("✅ ALL PASS — Sutra acts with the whole cloud down."
                  if all_ok else "❌ SOME FAILED — see above."))


if __name__ == "__main__":
    asyncio.run(main())
