"""Autonomous web agent: Sutra drives her own visible browser toward a goal.

browser-use's *pattern* — show the model the page's interactive elements, let it
pick one action, execute, repeat — over our Playwright + free MEDIUM router, with no
heavy browser-use/langchain dependency and no paid-OpenAI default ($0). Unlike the
first version it acts in Sutra's **persistent, visible** browser
(`friday.skills.browser_session`), so it's already logged in (you signed in once by
hand) and you watch it work on screen instead of in a hidden headless tab.

It clicks, types, and submits over several autonomous steps, so `web_task` is
auto_confirm=False — you approve the run up front (and a sub-agent declines it),
exactly like fill_form / run_command.
"""

from __future__ import annotations

import json
import re
from typing import Any

from friday.core.router import Tier, complete
from friday.skills.browse import _NAV_TIMEOUT_MS, _best_text, _http_url
from friday.skills.browser_session import get_page
from friday.skills.registry import skill

_AGENT_PROMPT = """You are Sutra, driving a real web browser to accomplish a goal. \
Each turn you see the page (URL, title, visible text) and a numbered list of its \
interactive elements. Reply with ONLY one JSON object — no prose:
  {"action":"click","index":N}             click element N
  {"action":"type","index":N,"text":"…"}   type text into element N
  {"action":"scroll","amount":700}         scroll down (negative = up) to reveal more
  {"action":"goto","url":"https://…"}      navigate to a URL
  {"action":"done","answer":"…"}           finish and answer the goal
Rules: take the fewest steps. Prefer clicking the listed elements over guessing URLs. \
The instant the page text already answers the goal, use "done". If you hit a \
login/sign-in wall with no way through, use "done" and say the user must log in to \
Sutra's browser first. Indices refer ONLY to the current list."""

_OBSERVE_SELECTOR = "a[href], button, input, textarea, select, [role=button], [onclick]"


async def _observe(page: Any, cap: int = 40) -> tuple[list[Any], list[str]]:
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


async def _step(page: Any, handles: list[Any], action: dict[str, Any]) -> str | None:
    """Execute one parsed action against the page. Returns an error string or None."""
    kind = action.get("action")
    try:
        if kind == "click":
            await handles[int(action["index"])].click(timeout=8000)
        elif kind == "type":
            await handles[int(action["index"])].fill(str(action.get("text", "")))
        elif kind == "scroll":
            await page.mouse.wheel(0, int(action.get("amount", 700)))
        elif kind == "goto" and _http_url(action.get("url", "")):
            await page.goto(
                action["url"], wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS
            )
        else:
            return f"unsupported or malformed action: {kind}"
    except Exception as e:  # noqa: BLE001 — bad index / element gone; let the loop retry
        return str(e)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:  # noqa: BLE001 — AJAX / no navigation
        pass
    return None


@skill(
    name="web_task",
    description=(
        "Autonomously DO a multi-step task on the web in Sutra's own visible browser: "
        "from a starting URL, she reads each page and clicks/types/scrolls/navigates "
        "over several steps to achieve the goal, then reports what she found or did. "
        "Use this for anything beyond opening a page — searching, navigating a site, "
        "filling things in, completing a flow. Works on logged-in sites if the user "
        "signed in once via Sutra's browser. IRREVERSIBLE (it may click/submit) — "
        "requires confirmation."
    ),
    auto_confirm=False,
    timeout_s=240,
)
async def web_task(goal: str, start_url: str, max_steps: int = 12) -> dict[str, Any]:
    """Drive Sutra's browser autonomously toward a goal.

    goal: What to accomplish or find (e.g. "open the first room and start it").
    start_url: The http(s) page to start from.
    max_steps: Maximum autonomous steps before giving up (1-25, default 12).
    """
    goal = goal.strip()
    if not goal:
        return {"error": "no goal given"}
    if not _http_url(start_url):
        return {"error": "start_url must be an http(s) URL", "start_url": start_url}
    steps_cap = max(1, min(25, max_steps))

    try:
        page = await get_page()
        await page.goto(start_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
    except Exception as e:  # noqa: BLE001 — browser launch / first nav failed
        return {"error": f"could not start Sutra's browser at {start_url}: {e}"}

    trace: list[dict[str, Any]] = []
    for _ in range(steps_cap):
        handles, lines = await _observe(page)
        # main-content text (not raw body) so the model sees the article, not nav chrome
        text = (await _best_text(page))[:1800]
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
                messages, tier=Tier.MEDIUM, tools=None, max_tokens=250, think=False
            )
        except Exception as e:  # noqa: BLE001 — no free key / provider down
            return {"error": f"web agent LLM unavailable: {e}", "trace": trace, "url": page.url}

        action = _parse_action(r.choices[0].message.content or "")
        if not action:
            trace.append({"error": "unparseable action"})
            break
        trace.append({"action": action})
        if action.get("action") == "done":
            return {
                "goal": goal, "answer": action.get("answer", ""),
                "url": page.url, "steps": len(trace), "trace": trace,
            }
        err = await _step(page, handles, action)
        if err:
            trace[-1]["error"] = err

    return {"goal": goal, "answer": "(max steps reached)", "url": page.url,
            "steps": len(trace), "trace": trace}
