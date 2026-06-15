"""Widget spec schema and deterministic layout composer for Generative UI.

A layout is an ordered list of typed widget specs (pydantic v2). The composer
maps structured tool output deterministically (no LLM) where the shape is
recognisable, and falls back to a single free-LLM JSON call (litellm, free tier)
for free-form answers. Any bad/empty spec collapses to a safe markdown widget.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger(__name__)

# ── Widget leaf models ──────────────────────────────────────────────────────

class MarkdownWidget(BaseModel):
    type: Literal["markdown"] = "markdown"
    text: str = ""


class TableWidget(BaseModel):
    type: Literal["table"] = "table"
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)

    @field_validator("rows", mode="before")
    @classmethod
    def coerce_rows(cls, v: Any) -> list[list[str]]:
        if not isinstance(v, list):
            return []
        return [[str(c) for c in row] if isinstance(row, list) else [str(row)] for row in v]


class KeyValueWidget(BaseModel):
    type: Literal["key_value"] = "key_value"
    pairs: list[dict[str, str]] = Field(default_factory=list)  # [{label, value}, ...]

    @field_validator("pairs", mode="before")
    @classmethod
    def coerce_pairs(cls, v: Any) -> list[dict[str, str]]:
        if not isinstance(v, list):
            return []
        out = []
        for item in v:
            if isinstance(item, dict):
                out.append({"label": str(item.get("label", "")),
                            "value": str(item.get("value", ""))})
        return out


class CodeWidget(BaseModel):
    type: Literal["code"] = "code"
    language: str = "text"
    text: str = ""


class BarChartWidget(BaseModel):
    type: Literal["bar_chart"] = "bar_chart"
    title: str = ""
    labels: list[str] = Field(default_factory=list)
    values: list[float] = Field(default_factory=list)
    unit: str = ""

    @model_validator(mode="after")
    def align_lengths(self) -> BarChartWidget:
        n = min(len(self.labels), len(self.values))
        self.labels = self.labels[:n]
        self.values = self.values[:n]
        return self


class CardItem(BaseModel):
    title: str = ""
    subtitle: str = ""
    url: str = ""


class CardsWidget(BaseModel):
    type: Literal["cards"] = "cards"
    items: list[CardItem] = Field(default_factory=list)

    @field_validator("items", mode="before")
    @classmethod
    def coerce_items(cls, v: Any) -> list[dict[str, str]]:
        if not isinstance(v, list):
            return []
        out = []
        for item in v:
            if isinstance(item, dict):
                out.append({
                    "title": str(item.get("title", "")),
                    "subtitle": str(item.get("subtitle", item.get("url", ""))),
                    "url": str(item.get("url", "")),
                })
        return out


class MetricWidget(BaseModel):
    type: Literal["metric"] = "metric"
    label: str = ""
    value: str = ""
    unit: str = ""


# ── Layout ─────────────────────────────────────────────────────────────────

AnyWidget = (
    MarkdownWidget | TableWidget | KeyValueWidget
    | CodeWidget | BarChartWidget | CardsWidget | MetricWidget
)

_WIDGET_TYPES: dict[str, type[AnyWidget]] = {
    "markdown": MarkdownWidget,
    "table": TableWidget,
    "key_value": KeyValueWidget,
    "code": CodeWidget,
    "bar_chart": BarChartWidget,
    "cards": CardsWidget,
    "metric": MetricWidget,
}


class Layout(BaseModel):
    """An ordered list of widget specs for one query/reply."""
    widgets: list[AnyWidget] = Field(default_factory=list)

    @classmethod
    def fallback(cls, text: str) -> Layout:
        """Safe fallback: wrap any text in a markdown widget."""
        return cls(widgets=[MarkdownWidget(text=text or "(no content)")])

    @classmethod
    def from_raw(cls, raw: list[dict[str, Any]]) -> Layout:
        """Parse a raw list of dicts into a Layout, falling back on any error."""
        widgets: list[AnyWidget] = []
        for spec in raw:
            wtype = spec.get("type", "")
            model_cls = _WIDGET_TYPES.get(wtype)
            if model_cls is None:
                continue
            try:
                widgets.append(model_cls.model_validate(spec))
            except Exception:  # noqa: BLE001
                log.debug("widget parse failed for %r", spec)
        if not widgets:
            return cls.fallback("")
        return cls(widgets=widgets)

    def to_json(self) -> list[dict[str, Any]]:
        return [w.model_dump() for w in self.widgets]


# ── Deterministic shape-detectors ──────────────────────────────────────────

def _looks_like_system_info(data: dict[str, Any]) -> bool:
    """system_info returns a dict with cpu_pct / ram_pct / disk_pct at minimum."""
    return isinstance(data, dict) and any(
        k in data for k in ("cpu_pct", "ram_pct", "disk_pct", "gpu_pct")
    )


def _looks_like_search_results(data: Any) -> bool:
    """SearXNG / web_search returns a list of dicts with url/title."""
    return (
        isinstance(data, list) and
        len(data) > 0 and
        isinstance(data[0], dict) and
        any(k in data[0] for k in ("url", "title", "link"))
    )


def _looks_like_key_value(data: dict[str, Any]) -> bool:
    """A flat dict of scalar values → key_value widget."""
    if not isinstance(data, dict):
        return False
    return all(isinstance(v, (str, int, float, bool, type(None))) for v in data.values())


def _detect_code(text: str) -> tuple[bool, str]:
    """Return (is_code, language) if the text looks like a code block."""
    m = re.match(r"^```(\w*)\n(.*?)```$", text, re.DOTALL)
    if m:
        return True, m.group(1) or "text"
    if re.search(r"\bdef \w+\(|class \w+[:(]|import \w+|#include|function \w+\(", text):
        return True, "python"
    return False, ""


# ── Composer ───────────────────────────────────────────────────────────────

def compose_layout(
    answer_text: str,
    tool_data: Any = None,
) -> Layout:
    """Map an agent answer + optional tool output to a Layout.

    Decision priority:
      1. tool_data is system_info dict → metric widgets + markdown summary
      2. tool_data is search/browse results list → cards widget
      3. tool_data is flat key-value dict → key_value widget
      4. answer_text looks like a code block → code widget
      5. Free-LLM JSON compose (litellm, free Groq) → validated Layout
      6. Fallback: single markdown widget
    """
    # 1. system_info → metrics
    if tool_data and _looks_like_system_info(tool_data):
        return _layout_system_info(tool_data, answer_text)

    # 2. search results → cards
    if tool_data and _looks_like_search_results(tool_data):
        return _layout_search(tool_data, answer_text)

    # 3. flat key-value dict → key_value
    if tool_data and _looks_like_key_value(tool_data):
        return _layout_key_value(tool_data, answer_text)

    # 4. code block in the answer itself
    if answer_text:
        is_code, lang = _detect_code(answer_text.strip())
        if is_code:
            inner = re.sub(r"^```\w*\n|```$", "", answer_text.strip(), flags=re.DOTALL)
            return Layout(widgets=[CodeWidget(language=lang, text=inner.strip())])

    # 5. free-LLM compose
    if answer_text:
        llm_layout = _llm_compose(answer_text, tool_data)
        if llm_layout is not None:
            return llm_layout

    # 6. markdown fallback
    return Layout.fallback(answer_text)


def _layout_system_info(data: dict[str, Any], summary: str) -> Layout:
    """Convert system_info output into metric + key_value widgets."""
    _METRIC_MAP = {
        "cpu_pct": ("CPU", "%"),
        "ram_pct": ("RAM", "%"),
        "disk_pct": ("Disk", "%"),
        "gpu_pct": ("GPU", "%"),
    }
    widgets: list[AnyWidget] = []
    for key, (label, unit) in _METRIC_MAP.items():
        if key in data:
            widgets.append(MetricWidget(label=label, value=str(data[key]), unit=unit))

    # any remaining scalar fields → key_value
    leftover = {k: v for k, v in data.items()
                if k not in _METRIC_MAP and isinstance(v, (str, int, float, bool))}
    if leftover:
        widgets.append(KeyValueWidget(pairs=[
            {"label": k, "value": str(v)} for k, v in leftover.items()
        ]))

    if summary:
        widgets.append(MarkdownWidget(text=summary))
    return Layout(widgets=widgets) if widgets else Layout.fallback(summary)


def _layout_search(results: list[dict[str, Any]], summary: str) -> Layout:
    """Convert search results list into a cards widget."""
    raw_items: list[dict[str, str]] = []
    for r in results[:8]:  # cap at 8 cards
        raw_items.append({
            "title": str(r.get("title", r.get("name", ""))),
            "subtitle": str(r.get("content", r.get("snippet", r.get("description", "")))),
            "url": str(r.get("url", r.get("link", ""))),
        })
    widgets: list[AnyWidget] = [CardsWidget(items=raw_items)]
    if summary:
        widgets.insert(0, MarkdownWidget(text=summary))
    return Layout(widgets=widgets)


def _layout_key_value(data: dict[str, Any], summary: str) -> Layout:
    pairs = [{"label": k, "value": str(v)} for k, v in data.items()]
    widgets: list[AnyWidget] = [KeyValueWidget(pairs=pairs)]
    if summary:
        widgets.insert(0, MarkdownWidget(text=summary))
    return Layout(widgets=widgets)


def _llm_compose(answer_text: str, tool_data: Any) -> Layout | None:
    """Ask the free Groq LLM to produce a widget layout JSON. Returns None on any failure."""
    try:
        import litellm  # type: ignore[import-untyped]
    except ImportError:
        return None

    system_prompt = (
        "You are a UI composer for a JARVIS-style AI dashboard (gold-on-obsidian aesthetic). "
        "Given an AI assistant's answer, return ONLY a JSON array of widget specs — no prose, "
        "no markdown fences, just the array. Each widget must have a `type` field.\n\n"
        "Available types and their required fields:\n"
        "- {\"type\":\"markdown\",\"text\":\"...\"}\n"
        "- {\"type\":\"key_value\",\"pairs\":[{\"label\":\"...\",\"value\":\"...\"}]}\n"
        "- {\"type\":\"metric\",\"label\":\"...\",\"value\":\"...\",\"unit\":\"...\"}\n"
        "- {\"type\":\"code\",\"language\":\"python\",\"text\":\"...\"}\n"
        "- {\"type\":\"bar_chart\",\"title\":\"...\",\"labels\":[],\"values\":[],\"unit\":\"\"}\n"
        "- {\"type\":\"table\",\"columns\":[],\"rows\":[[]]}\n"
        "- {\"type\":\"cards\",\"items\":"
        "[{\"title\":\"...\",\"subtitle\":\"...\",\"url\":\"\"}]}\n\n"
        "Rules: prefer a richer layout over plain markdown when the content has structure. "
        "Prefer 1-3 widgets; never add empty widgets. "
        "If the answer is plain prose, use a single markdown widget. "
        "Return ONLY the JSON array."
    )

    user_msg = answer_text
    if tool_data:
        user_msg += f"\n\n[Tool output]: {json.dumps(tool_data)[:800]}"

    try:
        resp = litellm.completion(
            model="groq/llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg[:2000]},
            ],
            max_tokens=600,
            temperature=0.1,
        )
        raw_text = resp.choices[0].message.content or ""
        # strip any accidental fences
        raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip())
        parsed = json.loads(raw_text)
        if not isinstance(parsed, list):
            return None
        layout = Layout.from_raw(parsed)
        # if we got only an empty-text fallback back, return None so caller falls back
        if (len(layout.widgets) == 1 and isinstance(layout.widgets[0], MarkdownWidget)
                and not layout.widgets[0].text):
            return None
        return layout
    except Exception:  # noqa: BLE001
        log.debug("LLM compose failed", exc_info=True)
        return None
