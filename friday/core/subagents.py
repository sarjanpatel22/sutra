"""Sub-agent delegation — the Claude Agent SDK *pattern*, over our own router.

A specialist sub-agent works a focused sub-task in an **isolated context window**
(it never sees the parent conversation), with a **restricted tool subset**, then
reports a single synthesized answer back. This is the SDK's delegation pattern
without the dependency: claude-agent-sdk wraps the Claude Code CLI subprocess and
is Claude-only, which would break Sutra's free-by-default local-tier routing.

Sub-agents run with no confirm hook, so irreversible skills are declined — a
delegated agent can read, search, compute, and inspect, but never writes or runs
commands unattended.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from friday.core.router import Tier, complete
from friday.skills import registry


@dataclass(frozen=True)
class SubAgent:
    name: str
    role: str
    system_prompt: str
    tier: Tier = Tier.MEDIUM
    # Whitelist of skill names this specialist may call. None = all skills.
    tools: frozenset[str] | None = field(default=None)
    max_rounds: int = 4
    # Cheap helper: never let a rate-limited worker escalate to a paid Claude tier ($0).
    free_only: bool = True


_ARCHITECT = SubAgent(
    name="architect",
    role="software architect — decomposition, trade-offs, a concrete plan",
    system_prompt=(
        "You are a senior software architect. Given a problem, break it down, weigh "
        "the real trade-offs, and return a concrete, ordered plan. Be decisive and "
        "specific; name the approach you'd pick and why. No filler."
    ),
    tools=frozenset({"calc", "recall"}),
)

_RESEARCHER = SubAgent(
    name="researcher",
    role="researcher — gather facts via search/memory and synthesize",
    system_prompt=(
        "You are a research specialist. Use the available tools (web search, memory, "
        "clock, weather) to gather real, current facts, then synthesize a tight, "
        "sourced answer. Prefer evidence over guessing; say so if you can't verify."
    ),
    tools=frozenset({"web_search", "recall", "current_time", "weather"}),
)

_CREATIVE = SubAgent(
    name="creative",
    role="creative — drafting, ideation, naming, prose",
    system_prompt=(
        "You are a creative specialist — drafting, ideation, naming, and prose. Generate "
        "concrete, vivid options and commit to a strong one; don't hedge or list endless "
        "alternatives. Match the asked-for intent and tone. No filler, no preamble."
    ),
    tools=frozenset({"recall"}),
)

_WATCHDOG = SubAgent(
    name="watchdog",
    role="systems watchdog — inspect machine/health state, report anomalies",
    system_prompt=(
        "You are a systems watchdog. Inspect system and health state with the tools "
        "you have, and report plainly: what's normal, what's off, and what (if "
        "anything) needs attention. Be terse and factual — no alarmism."
    ),
    tools=frozenset({"system_info", "battery"}),
)

SUBAGENTS: dict[str, SubAgent] = {
    a.name: a for a in (_ARCHITECT, _RESEARCHER, _CREATIVE, _WATCHDOG)
}


def _tools_for(sa: SubAgent) -> list[dict] | None:
    """The OpenAI tool schemas this sub-agent is allowed to call (None = all)."""
    if sa.tools is None:
        return registry.all_tools() or None
    allowed = [t for t in registry.all_tools() if t["function"]["name"] in sa.tools]
    return allowed or None


async def run_subagent(name: str, task: str, context: str = "") -> str:
    """Run a named specialist on a focused task in isolated context. Returns its
    final answer (or a clear error string for an unknown agent)."""
    from friday.core.agent import _run_tool_calls  # local import avoids a cycle

    registry.autodiscover()
    sa = SUBAGENTS.get(name.lower().strip())
    if sa is None:
        return f"Unknown sub-agent '{name}'. Available: {', '.join(SUBAGENTS)}."

    tools = _tools_for(sa)
    prompt = task if not context else f"{task}\n\nContext:\n{context}"
    messages: list[dict] = [
        {"role": "system", "content": sa.system_prompt},
        {"role": "user", "content": prompt},
    ]

    for _ in range(sa.max_rounds):
        response = await complete(messages, tier=sa.tier, tools=tools, free_only=sa.free_only)
        msg = response.choices[0].message
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": getattr(msg, "tool_calls", None),
        })
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return (msg.content or "").strip()
        # No confirm hook → irreversible skills are declined; `allowed` enforces the
        # tool whitelist at execution too, so an off-list call is blocked, not run.
        messages.extend(await _run_tool_calls(tool_calls, allowed=sa.tools))

    # Out of rounds — force a final answer with no further tools.
    response = await complete(messages, tier=sa.tier, tools=None, free_only=sa.free_only)
    return (response.choices[0].message.content or "").strip()


def list_subagents() -> list[tuple[str, str]]:
    """(name, role) for each registered specialist — used by the CLI / delegate skill."""
    return [(a.name, a.role) for a in SUBAGENTS.values()]
