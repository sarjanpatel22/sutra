"""Autonomous web agent: the browser-use *pattern* over our Playwright + free router.

Like the sub-agent layer takes the Claude-Agent-SDK pattern over our own router
(not the SDK), this takes browser-use's idea — show the LLM the page's interactive
elements, let it pick one action, execute, repeat — without the heavy browser-use +
langchain dependency or its paid-OpenAI default. The action LLM is our free MEDIUM
tier (Groq/Gemini), so the whole loop stays $0.

It can click and submit anything over several autonomous steps, so `web_task` is
auto_confirm=False — the user confirms the whole run up front (and a sub-agent
declines it), exactly like fill_form / run_command. Reuses browse.py's render helper.
"""

from __future__ import annotations

import json
import re
from typing import Any

from friday.core.router import Tier, complete
from friday.skills.browse import _NAV_TIMEOUT_MS, _http_url, _readable, _render
from friday.skills.registry import skill

_AGENT_PROMPT = """You are a web automation agent controlling a real browser. Each turn \
you see the current page (URL, title, visible text) and a numbered list of its \
interactive elements. Reply with ONLY one JSON object — no prose:
  {"action":"click","index":N}             click element N
  {"action":"type","index":N,"text":"…"}   type text into element N
  {"action":"goto","url":"https://…"}      navigate to a URL
  {"action":"done","answer":"…"}           finish and answer the goal
Use the fewest steps. The instant the page text already answers the goal, use "done". \
Indices refer ONLY to the current list."""

_OBSERVE_SELECTOR = "a[href], button, input, textarea, select, [role=button]"


async def _observe(page: Any, cap: int = 35) -> tuple[list[Any], list[str]]:
    """Indexed list of the page's visible interactive elements: (handles, lines)."""
    handles: list[Any] = []
    lines: list[str] = []
    for el in await page.query_selector_all(_OBSERVE_SELECTOR):
        try:
            if not await el.is_visible():
                continue
            tag = (await el.evaluate("e => e.tagName")).lower()
            label = (await el.inner_text() or "").strip()
            if not label:
                for a in ("aria-label", "placeholder", "value", "name", "title"):
                    label = await el.get_attribute(a) or ""
                    if label:
                        break
        except Exception:  # noqa: BLE001 — detached/odd node; skip it
            continue
        lines.append(f"[{len(handles)}] <{tag}> {' '.join(label.split())[:80]}")
        handles.append(el)
        if len(handles) >= cap:
            break
    return handles, lines


def _parse_action(text: str) -> dict[str, Any] | None:
    """Extract the first JSON action object from the LLM reply, or None."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) and "action" in obj else None


@skill(
    name="web_task",
    description=(
        "Autonomously complete a goal on the web: starting from a URL, an agent reads "
        "each page and clicks/types/navigates over several steps to achieve the goal, "
        "then reports what it found or did. IRREVERSIBLE (it may click/submit) — "
        "requires confirmation."
    ),
    auto_confirm=False,
    timeout_s=150,
)
async def web_task(goal: str, start_url: str, max_steps: int = 8) -> dict[str, Any]:
    """Drive a browser autonomously toward a goal.

    goal: What to accomplish or find (e.g. "find the price of the first listing").
    start_url: The http(s) page to start from.
    max_steps: Maximum autonomous steps before giving up (1-15, default 8).
    """
    goal = goal.strip()
    if not goal:
        return {"error": "no goal given"}
    if not _http_url(start_url):
        return {"error": "start_url must be an http(s) URL", "start_url": start_url}
    steps_cap = max(1, min(15, max_steps))

    async def _drive(page: Any, _resp: Any) -> dict[str, Any]:
        trace: list[dict[str, Any]] = []
        for _ in range(steps_cap):
            handles, lines = await _observe(page)
            text = _readable(await page.inner_text("body"))[:1500]
            obs = (
                f"URL: {page.url}\nTITLE: {await page.title()}\n\n"
                f"PAGE TEXT (truncated):\n{text}\n\nINTERACTIVE ELEMENTS:\n" + "\n".join(lines)
            )
            messages = [
                {"role": "system", "content": _AGENT_PROMPT},
                {"role": "user", "content": f"GOAL: {goal}\n\n{obs}\n\nNext action as JSON:"},
            ]
            try:
                r = await complete(
                    messages, tier=Tier.MEDIUM, tools=None, max_tokens=220, think=False
                )
            except Exception as e:  # noqa: BLE001 — no free key / provider down
                return {"error": f"web agent LLM unavailable: {e}", "trace": trace, "url": page.url}

            action = _parse_action(r.choices[0].message.content or "")
            if not action:
                trace.append({"error": "unparseable action"})
                break
            trace.append({"action": action})
            kind = action.get("action")
            if kind == "done":
                return {
                    "goal": goal, "answer": action.get("answer", ""),
                    "url": page.url, "steps": len(trace), "trace": trace,
                }
            try:
                if kind == "click":
                    await handles[int(action["index"])].click(timeout=8000)
                elif kind == "type":
                    await handles[int(action["index"])].fill(str(action.get("text", "")))
                elif kind == "goto" and _http_url(action.get("url", "")):
                    await page.goto(
                        action["url"], wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS
                    )
                else:
                    trace[-1]["error"] = f"unsupported or malformed action: {kind}"
                    continue
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:  # noqa: BLE001 — AJAX / no navigation
                    pass
            except Exception as e:  # noqa: BLE001 — bad index / element gone; let it retry
                trace[-1]["error"] = str(e)
        return {"goal": goal, "answer": "(max steps reached)", "url": page.url,
                "steps": len(trace), "trace": trace}

    return await _render(start_url, _drive)
