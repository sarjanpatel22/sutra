"""Council skill — let the main agent convene specialists on a hard, multi-faceted goal."""

from __future__ import annotations

from friday.skills.registry import skill


@skill(
    name="council",
    description=(
        "Convene a council of specialist sub-agents on a hard, multi-faceted goal: an "
        "architect decomposes it, then researcher / creative / watchdog work the parts in "
        "parallel (each in isolated context), and their work is reviewed and synthesized "
        "into one answer. Use for big 'research AND design AND draft' problems — not for "
        "simple single-tool turns (use the right skill directly for those)."
    ),
    auto_confirm=True,  # specialists inside still decline irreversible skills
    timeout_s=240,
)
async def council(goal: str, context: str = "") -> dict[str, str]:
    """Run the full council on a goal and return the synthesized answer.

    goal: the multi-faceted problem to solve.
    context: optional extra background the council should know.
    """
    from friday.core.council import convene

    result = await convene(goal, context)
    return {"answer": result.answer, "critique": result.critique}
