"""Scored tool-calling eval — how reliably does a LOCAL model pick the right skill?

This is the yardstick for the offline-brain fine-tune: before installing a 5 GB
training stack, measure whether base qwen2.5 (and friends) actually NEED tuning, and
prove any fine-tune beats the base. Each case is (utterance → acceptable tool names),
or None for a chat turn that must fire NO tool (false-positive guard). Runs against
Ollama via litellm — $0, no torch.

    .venv/bin/python scripts/eval_toolcalling.py
    BENCH_TOOL_MODELS="ollama_chat/qwen2.5:7b" .venv/bin/python scripts/eval_toolcalling.py
"""

from __future__ import annotations

import asyncio
import os

# (utterance, acceptable tool names | None-for-chat). Accept a SET where more than one
# tool is a legitimate pick, so we score intent, not one exact string.
_CASES: tuple[tuple[str, set[str] | None], ...] = (
    ("what time is it right now", {"current_time"}),
    ("what's today's date", {"current_time"}),
    ("what's the weather in Toronto", {"weather"}),
    ("is it going to rain tomorrow", {"weather"}),
    ("how much RAM does this machine have", {"system_info"}),
    ("what's my CPU and disk usage", {"system_info"}),
    ("what's my battery at", {"battery"}),
    ("am I plugged in", {"battery"}),
    ("what is 15 times 27", {"calc"}),
    ("what's the square root of 4096", {"calc"}),
    ("search the web for the latest AI news", {"web_search", "web_task", "web_task_vision"}),
    ("look up who won the world cup in 2022", {"web_search", "web_task", "web_task_vision"}),
    ("open firefox", {"open_app"}),
    ("launch the terminal", {"open_app"}),
    ("open github.com", {"open_url", "browser_open", "open_app"}),
    ("go to youtube.com", {"open_url", "browser_open"}),
    ("what windows do I have open", {"list_windows"}),
    ("switch to my editor", {"focus_window"}),
    ("close the firefox window", {"close_window"}),
    ("what's in my clipboard", {"clipboard_get"}),
    ("copy hello world to my clipboard", {"clipboard_set"}),
    ("what do you remember about my job", {"recall"}),
    ("what do you know about my projects", {"recall"}),
    ("remember that I prefer celsius", {"set_preference", "remember"}),
    ("run ls -la in the terminal", {"run_command"}),
    ("read the file /etc/hostname", {"read_file"}),
    ("list the files in my home directory", {"list_directory"}),
    ("what's the git status here", {"git_status"}),
    # chat negatives — must fire NO tool
    ("how are you doing today", None),
    ("tell me a joke", None),
    ("thanks, that was helpful", None),
    ("what do you think is the meaning of life", None),
    ("explain how photosynthesis works", None),
)

_SYS = (
    "You are Sutra. You have tools that DO things. When the user asks you to fetch, "
    "open, run, search, compute, or recall something real, CALL the single matching "
    "tool. If it's just chitchat or general knowledge, answer normally with NO tool."
)


def _names(tool_calls: list) -> list[str]:
    out = []
    for tc in tool_calls or []:
        fn = getattr(tc, "function", tc)
        out.append(getattr(fn, "name", ""))
    return out


async def _eval_model(model: str, tools: list) -> tuple[int, int]:
    import litellm

    correct = 0
    for utt, expected in _CASES:
        try:
            r = await litellm.acompletion(
                model=model,
                messages=[{"role": "system", "content": _SYS}, {"role": "user", "content": utt}],
                tools=tools, temperature=0.0, max_tokens=200,
            )
            got = _names(getattr(r.choices[0].message, "tool_calls", None))
        except Exception as e:  # noqa: BLE001
            print(f"  [err ] {utt[:42]:44} {str(e)[:50]}")
            continue
        if expected is None:
            ok = not got
            tag = "chat" if ok else f"FIRED {got}"
        else:
            ok = bool(got) and got[0] in expected
            tag = f"{got[0] if got else 'NONE'}"
        correct += ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {utt[:42]:44} -> {tag}")
    return correct, len(_CASES)


async def main() -> None:
    from friday.core.secrets import load_into_env
    from friday.skills import registry

    load_into_env()
    registry.autodiscover()
    tools = registry.all_tools()
    raw = os.getenv("BENCH_TOOL_MODELS", "ollama_chat/qwen2.5:7b,ollama_chat/qwen2.5:14b,"
                                          "ollama_chat/llama3.2:3b")
    for model in [m.strip() for m in raw.split(",") if m.strip()]:
        print(f"\n============ {model} ============")
        correct, total = await _eval_model(model, tools)
        print(f"  --> {correct}/{total} = {100*correct/total:.0f}% tool-calling accuracy")


if __name__ == "__main__":
    asyncio.run(main())
