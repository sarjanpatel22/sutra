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
async def forget(query: str, confirm: bool = False) -> dict[str, object]:
    """Forget facts from memory.

    query: Describes the facts/episodes to remove.
    confirm: Must be true to actually delete. Default false previews the exact
        facts that would be removed without deleting anything — forget matches by
        meaning, so always review the preview before confirming.
    """
    store = get_store()
    if not confirm:
        preview = await store.forget(query, apply=False)
        return {
            "would_remove": preview["matched"],
            "count": len(preview["matched"]),
            "note": "Nothing was deleted. Review these, then call forget again "
                    "with confirm=true to remove them.",
        }
    result = await store.forget(query, apply=True)
    return {"removed_episodes": result["removed"], "removed_facts": result["matched"]}
