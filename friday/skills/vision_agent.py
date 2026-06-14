"""Screen-viewer agent: Sutra *looks at* the page and clicks what she sees.

Where `web_task` reads the DOM, this one works from a screenshot — the modern
"GUI agent" approach. It draws a numbered badge on every clickable spot
(Set-of-Mark), screenshots the page, and asks a free vision model (Groq
Llama-4-Scout, $0) to pick the *number* to act on. Picking a number is reliable
where raw pixel-coordinate guessing is not, and seeing pixels means it can read
modern React UIs the plain DOM reader struggles with.

It does NOT defeat bot-walls or CAPTCHAs (TryHackMe-style gates stay the user's
job) — it just gives Sutra eyes on normal web apps. Gated like web_task.
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any

import litellm

from friday.skills.browse import _NAV_TIMEOUT_MS, _http_url
from friday.skills.browser_session import get_page
from friday.skills.registry import skill

_VISION_MODEL = "groq/meta-llama/llama-4-scout-17b-16e-instruct"  # free, fast, multimodal

# Per-step settle: cap the wait so a busy SPA (ads/polling) can't burn 3.5s EVERY step.
# Measured: the old networkidle=3500 was the worst-case sink behind the 71s spin.
_SETTLE_MS = 1500
# JPEG (not PNG) screenshots: ~2x smaller, upload + vision-infer faster, same picks
# (measured: identical action chosen on PNG vs JPEG).
_JPEG_QUALITY = 70
# Hard wall-clock cap so a task NEVER grinds (he watched one spin 71s). Overridable.
_DEFAULT_BUDGET_S = 45

# Mark every visible clickable with a numbered badge and return [{idx,tag,label}].
# Reused each step; clears its own prior marks first so numbering stays in sync.
_MARK_JS = r"""(maxN) => {
  document.querySelectorAll('.sutra-mark').forEach(e => e.remove());
  document.querySelectorAll('[data-sutra-idx]').forEach(e => e.removeAttribute('data-sutra-idx'));
  const ROLES = new Set(['button','link','menuitem','tab','checkbox','radio','switch',
    'option','combobox','textbox','searchbox']);
  const out = []; let idx = 0;
  for (const e of document.querySelectorAll('*')) {
    if (idx >= maxN) break;
    const tag = e.tagName.toLowerCase();
    const role = e.getAttribute('role') || '';
    const ti = e.getAttribute('tabindex');
    let st; try { st = getComputedStyle(e); } catch (_) { continue; }
    const std = ['a','button','input','textarea','select'].includes(tag);
    const sem = std || ROLES.has(role) || e.hasAttribute('onclick') || (ti && ti !== '-1');
    if (!(sem || st.cursor === 'pointer')) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
    if (st.visibility === 'hidden' || st.display === 'none' ||
        parseFloat(st.opacity || '1') === 0) continue;
    const anc = e.closest('[data-sutra-idx]');
    if (anc && anc.innerText.trim() === (e.innerText || '').trim()) continue;
    e.setAttribute('data-sutra-idx', String(idx));
    const b = document.createElement('div');
    b.className = 'sutra-mark';
    b.textContent = idx;
    b.style.cssText = 'position:fixed;z-index:2147483647;background:#ff2d55;color:#fff;' +
      'font:bold 12px monospace;padding:0 3px;border-radius:3px;pointer-events:none;' +
      'left:' + Math.max(0, r.left) + 'px;top:' + Math.max(0, r.top) + 'px;';
    document.body.appendChild(b);
    let label = (e.innerText || '').trim() || e.getAttribute('aria-label') ||
                e.getAttribute('placeholder') || e.getAttribute('title') || '';
    out.push({idx, tag, label: label.replace(/\s+/g, ' ').slice(0, 60)});
    idx++;
  }
  return out;
}"""

_CLEAR_JS = "() => document.querySelectorAll('.sutra-mark').forEach(e => e.remove())"

_SYS = """You are Sutra, operating a web browser by LOOKING at a screenshot. Each \
clickable thing has a red number badge; you also get a list of those numbers with short \
labels.

FIRST, before clicking anything: if the GOAL's answer is already VISIBLE anywhere in the \
screenshot — body text, an infobox, a sidebar, a table — reply "done" with it right away. \
Do NOT click around to "confirm" what you can already read.

Otherwise pick ONE action to make progress. Reply with ONLY one JSON object:
  {"act":"click","n":N}                 click badge N
  {"act":"type","n":N,"text":"…","enter":true}  type into field N (enter to submit)
  {"act":"scroll","dy":600}             scroll (negative = up) to reveal off-screen content
  {"act":"done","answer":"…"}           the goal is met / answered — report it
"done" is the ONLY way to give an answer — never type your answer into the page. If acting \
left you on the same screen, try a DIFFERENT action (scroll to find it, or answer if you \
can now see it) — don't repeat a click that did nothing. If a CAPTCHA or human-check \
blocks you, use "done" and say so."""


async def _mark(page: Any, cap: int) -> list[dict[str, Any]]:
    try:
        return await page.evaluate(_MARK_JS, cap)
    except Exception:  # noqa: BLE001
        return []


def _parse(text: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) and "act" in obj else None


async def _handle(page: Any, idx: int) -> Any:
    return await page.query_selector(f"[data-sutra-idx='{idx}']")


async def _do(page: Any, marks: list[dict[str, Any]], a: dict[str, Any]) -> str | None:
    """Execute one vision action by badge number. Returns an error string or None."""
    act = a.get("act")
    try:
        if act == "scroll":
            await page.mouse.wheel(0, int(a.get("dy", 600)))
            return None
        if act in ("click", "type"):
            el = await _handle(page, int(a.get("n", -1)))
            if el is None:
                return f"no element numbered {a.get('n')}"
            if act == "click":
                await el.click(timeout=8000)
            else:
                await el.fill(str(a.get("text", "")))
                if a.get("enter"):
                    await el.press("Enter")
            return None
        return f"unknown action: {act}"
    except Exception as e:  # noqa: BLE001
        return str(e)


@skill(
    name="web_task_vision",
    description=(
        "Do a web task by SIGHT — the most reliable way to operate a website. Sutra "
        "screenshots the page, sees the numbered clickable elements, and clicks/types "
        "over several steps to reach a goal. PREFER THIS for any multi-step web task, "
        "search, or visual/React app (over web_task). Needs a starting URL. Does NOT "
        "bypass CAPTCHAs or bot-walls. IRREVERSIBLE — requires confirmation."
    ),
    auto_confirm=False,
    timeout_s=240,
)
async def web_task_vision(
    goal: str, start_url: str, max_steps: int = 12, max_seconds: float = _DEFAULT_BUDGET_S
) -> dict[str, Any]:
    """Drive Sutra's browser toward a goal using vision (screenshots + Set-of-Mark).

    max_seconds caps total wall-clock time so the task degrades honestly instead of
    grinding — it stops and says so rather than spinning on a stuck page.
    """
    goal = goal.strip()
    if not goal:
        return {"error": "no goal given"}
    if not _http_url(start_url):
        return {"error": "start_url must be an http(s) URL"}
    steps_cap = max(1, min(25, max_steps))
    deadline = time.monotonic() + max(10.0, max_seconds)

    try:
        page = await get_page()
        await page.goto(start_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not open {start_url}: {e}"}

    trace: list[dict[str, Any]] = []
    sigs: list[str] = []
    for _ in range(steps_cap):
        if time.monotonic() > deadline:
            return {"goal": goal, "answer": "I ran out of time on that page and stopped.",
                    "timed_out": True, "url": page.url, "steps": len(trace), "trace": trace}
        if len(sigs) >= 3 and len(set(sigs[-3:])) == 1:
            return {"goal": goal, "answer": "I got visually stuck on that page and stopped.",
                    "stuck": True, "url": page.url, "steps": len(trace), "trace": trace}
        try:
            await page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
        except Exception:  # noqa: BLE001
            pass
        marks = await _mark(page, cap=40)
        shot = base64.b64encode(
            await page.screenshot(type="jpeg", quality=_JPEG_QUALITY)
        ).decode()
        await page.evaluate(_CLEAR_JS)
        listing = "\n".join(f"{m['idx']}: <{m['tag']}> {m['label']}" for m in marks) or "(none)"
        user = [
            {"type": "text", "text": f"GOAL: {goal}\n\nNumbered elements:\n{listing}\n\n"
                                     "Look at the screenshot and reply with one JSON action:"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{shot}"}},
        ]
        try:  # free Groq vision; called directly (router has no multimodal tier)
            r = await litellm.acompletion(
                model=_VISION_MODEL,
                messages=[{"role": "system", "content": _SYS},
                          {"role": "user", "content": user}],
                max_tokens=200, temperature=0.1,
            )
        except Exception as e:  # noqa: BLE001
            return {"error": f"vision model unavailable: {e}", "trace": trace, "url": page.url}
        action = _parse(r.choices[0].message.content or "")
        if not action:
            trace.append({"error": "unparseable", "raw": (r.choices[0].message.content or "")[:80]})
            break
        trace.append({"action": action})
        if action.get("act") == "done":
            return {"goal": goal, "answer": action.get("answer", ""),
                    "url": page.url, "steps": len(trace), "trace": trace}
        sigs.append(json.dumps({k: action.get(k) for k in ("act", "n", "text", "dy")}))
        err = await _do(page, marks, action)
        if err:
            trace[-1]["error"] = err

    return {"goal": goal, "answer": "I couldn't finish that one by sight.",
            "url": page.url, "steps": len(trace), "trace": trace}
