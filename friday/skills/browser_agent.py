"""Autonomous web agent: Sutra drives her own visible browser toward a goal.

browser-use's *pattern* — show the model the page's interactive elements, let it
pick one action, execute, repeat — over our Playwright + free MEDIUM router, with no
heavy browser-use/langchain dependency and no paid-OpenAI default ($0). It acts in
Sutra's **persistent, visible** browser (`friday.skills.browser_session`), so it's
already logged in (you signed in once by hand) and you watch it work on screen.

Three things make it actually *complete* tasks instead of flailing:
  * **Memory** — each step the model sees a running log of what it already did and
    whether it worked, so it stops repeating clicks / scrolling forever.
  * **Submit** — `type` can press Enter, and there's a `press` action, so search
    and login boxes actually go somewhere.
  * **Patience** — it waits for SPA content (TryHackMe et al. render after load).

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
Each turn you see the page (URL, title, visible text), a numbered list of its \
interactive elements, and a log of what you've already done. Reply with ONLY one \
JSON object — no prose:
  {"action":"click","index":N}                     click element N
  {"action":"type","index":N,"text":"…"}           type text into element N
  {"action":"type","index":N,"text":"…","submit":true}  type then press Enter
  {"action":"press","key":"Enter"}                 press a key (Enter, Tab, Escape…)
  {"action":"scroll","amount":700}                 scroll down (negative = up)
  {"action":"goto","url":"https://…"}              navigate to a URL
  {"action":"done","answer":"…"}                   finish and report the answer
CRITICAL: "done" is the ONLY way to answer or report anything to the user. "type" \
puts text into a form field on the page — NEVER type your answer into the page. The \
moment you can see the answer in the PAGE TEXT, reply with done and put it in "answer".
Rules: take the fewest steps and MAKE PROGRESS — never repeat an action from the log \
that already ran; if the last steps changed nothing, try a different element or a \
new approach. After typing into a search/login field, submit it. On a search-results \
page, click the single most relevant result to open it — do NOT search again. The \
MOMENT the PAGE TEXT contains the answer to the goal, reply "done" with that answer — \
do not keep clicking. If you hit a login/sign-in wall with no way through, use "done" \
and say the user must log in to Sutra's browser first. Indices refer ONLY to the \
current element list."""

_OBSERVE_SELECTOR = "a[href], button, input, textarea, select, [role=button], [onclick]"

# Honest failure answers fed back to the speaker so it never pretends or blames the user.
_STUCK_MSG = (
    "I got stuck on that page — a button I kept trying didn't respond, so I stopped. "
    "The site may need a manual click."
)
_MAXED_MSG = (
    "I worked through several steps but couldn't finish that one on this site. "
    "It may need a hand, or a more specific instruction."
)
# input types that are never a useful target (prefs, toggles) — skip them outright.
_SKIP_INPUT_TYPES = {"hidden", "radio", "checkbox", "range", "color", "file"}
# is this element inside the page's main content (a result/article) vs nav/footer chrome?
_IN_MAIN_JS = (
    "e => !!e.closest('main, [role=main], article, #content, #mw-content-text, "
    "#bodyContent, .mw-search-results, .search-results, [role=search]')"
)


async def _collect_frame(frame: Any, is_subframe: bool,
                         primary: list[tuple[Any, str]], chrome: list[tuple[Any, str]]) -> None:
    """Pull interactive elements from one frame into primary/chrome buckets.

    Sites like TryHackMe render the real content (Start Machine, tasks) inside an
    iframe — its elements are always 'primary' (they ARE the embedded app)."""
    try:
        els = await frame.query_selector_all(_OBSERVE_SELECTOR)
    except Exception:  # noqa: BLE001 — cross-origin / detached frame
        return
    for el in els:
        try:
            if not await el.is_visible():
                continue
            tag = (await el.evaluate("e => e.tagName")).lower()
            if tag == "input":
                itype = (await el.get_attribute("type") or "text").lower()
                if itype in _SKIP_INPUT_TYPES:
                    continue  # theme/font radios and hidden fields are noise
            label = (await el.inner_text() or "").strip()
            if not label:
                for a in ("aria-label", "placeholder", "name", "title"):
                    label = await el.get_attribute(a) or ""
                    if label:
                        break
            val = await el.get_attribute("value") if tag in ("input", "textarea") else None
            in_main = is_subframe or bool(await el.evaluate(_IN_MAIN_JS))
        except Exception:  # noqa: BLE001 — detached/odd node; skip it
            continue
        extra = f" ={val!r}" if val else ""
        rendered = f"<{tag}> {' '.join(label.split())[:80]}{extra}"
        (primary if in_main else chrome).append((el, rendered))


async def _observe(page: Any, cap: int = 50) -> tuple[list[Any], list[str]]:
    """Indexed interactive elements, **content first** so real results aren't buried
    under nav/preference chrome — and traversing iframes so embedded app content
    (TryHackMe rooms, embedded forms) is actually visible. Returns (handles, lines)."""
    primary: list[tuple[Any, str]] = []   # main content + iframe (embedded app) controls
    chrome: list[tuple[Any, str]] = []    # nav/footer/header — kept only as filler
    await _collect_frame(page.main_frame, False, primary, chrome)
    for frame in page.frames:
        if frame is not page.main_frame:
            await _collect_frame(frame, True, primary, chrome)

    ordered = primary + chrome  # content links the model needs come first
    handles: list[Any] = []
    lines: list[str] = []
    for el, rendered in ordered[:cap]:
        lines.append(f"[{len(handles)}] {rendered}")
        handles.append(el)
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


def _signature(action: dict[str, Any]) -> str:
    """Stable key for loop detection — same action on the same target."""
    return json.dumps(
        {k: action.get(k) for k in ("action", "index", "text", "key", "url", "amount")},
        sort_keys=True,
    )


async def _settle(page: Any) -> None:
    """Wait for the page to stop loading — DOM first, then a short SPA/network grace."""
    for state, timeout in (("domcontentloaded", 8000), ("networkidle", 4000)):
        try:
            await page.wait_for_load_state(state, timeout=timeout)
        except Exception:  # noqa: BLE001 — AJAX never idles / no navigation; move on
            pass


async def _step(page: Any, handles: list[Any], action: dict[str, Any]) -> str | None:
    """Execute one parsed action against the page. Returns an error string or None."""
    kind = action.get("action")
    try:
        if kind == "click":
            await handles[int(action["index"])].click(timeout=8000)
        elif kind == "type":
            el = handles[int(action["index"])]
            await el.fill(str(action.get("text", "")))
            if action.get("submit"):
                await el.press("Enter")
        elif kind == "press":
            await page.keyboard.press(str(action.get("key", "Enter")))
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
    await _settle(page)
    return None


def _history_block(trace: list[dict[str, Any]]) -> str:
    """Render the recent action log the model reads each turn (its working memory)."""
    if not trace:
        return "WHAT YOU'VE ALREADY DONE: (nothing yet — this is step 1)"
    lines = []
    for i, t in enumerate(trace[-8:], 1):  # last 8 keeps the prompt small but informative
        a = t.get("action", {})
        desc = a.get("action", "?")
        for k in ("index", "text", "key", "url", "amount"):
            if k in a:
                desc += f" {k}={a[k]!r}"
        outcome = f"ERROR: {t['error']}" if t.get("error") else "ok"
        lines.append(f"{i}. {desc} → {outcome}")
    return "WHAT YOU'VE ALREADY DONE (do not repeat these pointlessly):\n" + "\n".join(lines)


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
async def web_task(
    goal: str, start_url: str, max_steps: int = 14, use_my_login: bool = True
) -> dict[str, Any]:
    """Drive Sutra's browser autonomously toward a goal.

    goal: What to accomplish or find (e.g. "open the first room and start it").
    start_url: The http(s) page to start from.
    max_steps: Maximum autonomous steps before giving up (1-25, default 14).
    use_my_login: Reuse your Firefox login for this site (default true) so she's
        signed in without a second login.
    """
    goal = goal.strip()
    if not goal:
        return {"error": "no goal given"}
    if not _http_url(start_url):
        return {"error": "start_url must be an http(s) URL", "start_url": start_url}
    steps_cap = max(1, min(25, max_steps))

    try:
        page = await get_page()
        if use_my_login:  # share your Firefox session before loading the site
            try:
                from friday.skills.firefox_login import apply_firefox_login, domain_of

                await apply_firefox_login(page.context, domain_of(start_url))
            except Exception:  # noqa: BLE001 — login reuse is best-effort, never blocks
                pass
        await page.goto(start_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        await _settle(page)
    except Exception as e:  # noqa: BLE001 — browser launch / first nav failed
        return {"error": f"could not start Sutra's browser at {start_url}: {e}"}

    trace: list[dict[str, Any]] = []
    recent_sigs: list[str] = []
    for _ in range(steps_cap):
        # Hard stop: 3 identical actions in a row means the page isn't responding to it
        # (TryHackMe et al.) — bail honestly in seconds instead of spinning to max steps.
        if len(recent_sigs) >= 3 and len(set(recent_sigs[-3:])) == 1:
            return {
                "goal": goal, "answer": _STUCK_MSG, "stuck": True,
                "url": page.url, "steps": len(trace), "trace": trace,
            }
        handles, lines = await _observe(page)
        # main-content text (not raw body) so the model sees the article, not nav chrome
        text = (await _best_text(page))[:1800]
        # after 2 identical actions, warn the model before the hard stop kicks in
        stuck = len(recent_sigs) >= 2 and len(set(recent_sigs[-2:])) == 1
        nudge = (
            "\n\nThat last action did nothing twice. Do NOT repeat it — pick a DIFFERENT "
            "element, scroll to find a better one, or finish with done."
            if stuck else ""
        )
        obs = (
            f"URL: {page.url}\nTITLE: {await page.title()}\n\n"
            f"{_history_block(trace)}{nudge}\n\n"
            f"PAGE TEXT (truncated):\n{text}\n\nINTERACTIVE ELEMENTS:\n" + "\n".join(lines)
        )
        messages = [
            {"role": "system", "content": _AGENT_PROMPT},
            {"role": "user", "content": f"GOAL: {goal}\n\n{obs}\n\nNext action as JSON:"},
        ]
        try:
            r = await complete(
                messages, tier=Tier.MEDIUM, tools=None,
                max_tokens=250, temperature=0.2, think=False,
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
        recent_sigs.append(_signature(action))
        err = await _step(page, handles, action)
        if err:
            trace[-1]["error"] = err

    return {"goal": goal, "answer": _MAXED_MSG, "url": page.url,
            "steps": len(trace), "trace": trace}
