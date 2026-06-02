"""Memory skills — recall and forget facts in long-term graph memory."""

from __future__ import annotations

from friday.memory.store import get_store
from friday.skills.registry import skill


@skill(
    name="recall",
    description="Search long-term memory for facts about a person, project, or topic.",
    auto_confirm=True,
    timeout_s=20,
)
async def recall(query: str, limit: int = 5) -> dict[str, object]:
    """Recall facts from memory.

    query: What to look up (a name, project, topic, or question).
    limit: Maximum number of facts to return (default 5).
    """
    facts = await get_store().recall(query, limit=limit)
    return {"facts": facts, "count": len(facts)}


@skill(
    name="forget",
    description="Remove remembered facts matching a query from long-term memory.",
    auto_confirm=False,  # destructive — confirm before deleting memories
    timeout_s=20,
)
async def forget(query: str) -> dict[str, object]:
    """Forget facts from memory.

    query: Describes the facts/episodes to remove.
    """
    removed = await get_store().forget(query)
    return {"removed_episodes": removed}
