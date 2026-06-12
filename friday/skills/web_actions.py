"""Gated web-action skills: fill and submit forms with a headless browser.

The read-only counterparts live in browse.py; these *change* state (a submit can POST
data), so they are auto_confirm=False — the agent confirms before running them, exactly
like write_file / run_command (and a sub-agent declines them outright, since it carries
no confirm hook). Reuses browse.py's headless-render helper so launch/navigation behave
identically to the read skills.
"""

from __future__ import annotations

from typing import Any

from friday.skills.browse import _MAX_CHARS, _readable, _render
from friday.skills.registry import skill


@skill(
    name="fill_form",
    description=(
        "Fill a web form's fields and optionally submit it. Give the page URL and a map "
        "of CSS selector -> value to type; pass a submit selector to click the submit "
        "button. IRREVERSIBLE (a submit may send data) — requires confirmation."
    ),
    auto_confirm=False,
    timeout_s=60,
)
async def fill_form(
    url: str, fields: dict[str, str], submit: str = "", max_chars: int = _MAX_CHARS
) -> dict[str, Any]:
    """Fill a web form and optionally submit it.

    url: An http:// or https:// URL with the form.
    fields: Map of CSS selector -> text to type into that field (text inputs/textareas).
    submit: CSS selector of the submit button to click. Empty = fill only, do not submit.
    max_chars: Truncate the resulting page's text to this many characters (default 6000).
    """
    if not isinstance(fields, dict):
        return {"error": "fields must be a map of CSS selector -> value", "url": url}
    chars = max(200, min(40_000, max_chars))
    items = list(fields.items())

    async def _extract(page: Any, resp: Any) -> dict[str, Any]:
        filled: list[str] = []
        for sel, val in items:
            try:
                await page.fill(sel, str(val))
            except Exception as e:  # noqa: BLE001 — selector missing / not fillable
                return {"error": f"could not fill {sel!r}: {e}", "url": page.url, "filled": filled}
            filled.append(sel)

        submitted = False
        if submit:
            try:
                await page.click(submit)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                except Exception:  # noqa: BLE001 — AJAX submit (no navigation) is fine
                    pass
                submitted = True
            except Exception as e:  # noqa: BLE001
                return {
                    "error": f"could not submit ({submit!r}): {e}",
                    "url": page.url,
                    "filled": filled,
                }

        return {
            "url": page.url,
            "status": resp.status if resp else None,
            "filled": filled,
            "submitted": submitted,
            "text": _readable(await page.inner_text("body"))[:chars],
        }

    return await _render(url, _extract)
