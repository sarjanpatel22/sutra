"""Scored reasoning benchmark — which FREE config is actually smartest on hard turns.

Unlike bench_models.py (tool-calling/latency), this GRADES correctness on questions
where a one-shot 70B commonly trips (bat-and-ball, 9.11 vs 9.9, sibling logic, day-of-
week arithmetic). Each model ends its reply with "ANSWER: <x>"; we parse that line and
score it against ground truth. The point: decide whether routing hard turns to a bigger
model or a reasoning pass genuinely raises accuracy — with a number, not a hunch.

    .venv/bin/python scripts/bench_reason.py
Override the line-up with BENCH_REASON="groq/...,gemini/..." (comma-separated).
"""

from __future__ import annotations

import asyncio
import os
import re

# (question, expected-answer). Numeric expecteds are compared as the first number in
# the model's ANSWER line; word expecteds as a substring.
_PROMPTS: tuple[tuple[str, str], ...] = (
    ("If 5 machines take 5 minutes to make 5 widgets, how many minutes do 100 machines "
     "take to make 100 widgets?", "5"),
    ("Which is larger: 9.11 or 9.9?", "9.9"),
    ("A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. "
     "How many cents does the ball cost?", "5"),
    ("Sarah has 2 brothers and 1 sister. How many sisters does each of Sarah's "
     "brothers have?", "2"),
    ("What is the next number in the sequence 2, 6, 12, 20, 30, ?", "42"),
    ("If today is Monday, what day of the week is it 100 days from now?", "wednesday"),
)
_SYS = "Reason carefully, then end your reply with a line exactly like 'ANSWER: <answer>'."

# Optional per-model extra kwargs (e.g. reasoning_effort). Default line-up below.
_MODELS: tuple[tuple[str, dict], ...] = (
    ("groq/llama-3.3-70b-versatile", {}),
    ("groq/openai/gpt-oss-120b", {}),
    ("groq/openai/gpt-oss-120b", {"reasoning_effort": "high"}),
    ("gemini/gemini-2.5-flash", {}),
)

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _extract_answer(text: str) -> str:
    m = re.search(r"answer\s*[:\-]\s*(.+)", text, re.I)
    return (m.group(1) if m else text).strip().lower()


def _correct(ans: str, expected: str) -> bool:
    if _NUM.fullmatch(expected):  # numeric → compare first number in the answer
        got = _NUM.search(ans)
        return bool(got and got.group() == expected)
    return expected.lower() in ans  # word → substring


async def _ask(model: str, extra: dict, question: str) -> tuple[str, str | None]:
    import litellm

    try:
        r = await litellm.acompletion(
            model=model,
            messages=[{"role": "system", "content": _SYS}, {"role": "user", "content": question}],
            max_tokens=900, temperature=0.0, **extra,
        )
        return (r.choices[0].message.content or ""), None
    except Exception as e:  # noqa: BLE001
        low = str(e).lower()
        return "", ("rate-limited" if "rate" in low or "too large" in low else str(e)[:60])


async def main() -> None:
    from friday.core.secrets import load_into_env

    load_into_env()
    line_up = os.getenv("BENCH_REASON")
    if line_up:
        models = [(m.strip(), {}) for m in line_up.split(",") if m.strip()]
    else:
        models = list(_MODELS)

    for model, extra in models:
        tag = f"{model}" + (f" (reasoning={extra['reasoning_effort']})" if extra else "")
        score = 0
        graded = 0
        print(f"\n============ {tag} ============")
        for q, expected in _PROMPTS:
            text, err = await _ask(model, extra, q)
            if err:
                print(f"  [skip] {q[:46]:48} {err}")
                await asyncio.sleep(2.5)
                continue
            ans = _extract_answer(text)
            ok = _correct(ans, expected)
            graded += 1
            score += ok
            print(f"  [{'PASS' if ok else 'FAIL'}] {q[:46]:48} got={ans[:24]!r} want={expected!r}")
            await asyncio.sleep(2.5)  # be gentle on the free tier
        if graded:
            print(f"  --> {score}/{graded} correct")


if __name__ == "__main__":
    asyncio.run(main())
