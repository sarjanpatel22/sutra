"""Delegate skill — let the main agent hand a sub-task to a specialist sub-agent."""

from __future__ import annotations

from friday.skills.registry import skill


@skill(
    name="delegate",
    description=(
        "Hand a focused sub-task to a specialist sub-agent that works it in isolated "
        "context and reports back. agent must be one of: architect (decomposition, "
        "trade-offs, a plan), researcher (gather + synthesize facts via search/memory), "
        "creative (drafting, ideation, naming, prose), watchdog (inspect system/health "
        "state). Use for self-contained sub-problems."
    ),
    auto_confirm=True,  # the sub-agent itself declines irreversible skills
    timeout_s=120,
)
async def delegate(agent: str, task: str, context: str = "") -> dict[str, str]:
    """Run a specialist sub-agent on a focused task.

    agent: one of "architect", "researcher", "watchdog".
    task: the self-contained sub-task to hand off.
    context: optional extra background the sub-agent should know.
    """
    from friday.core.subagents import run_subagent

    answer = await run_subagent(agent, task, context)
    return {"agent": agent, "result": answer}
