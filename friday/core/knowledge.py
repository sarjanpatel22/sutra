"""Fast, idempotent knowledge learning.

Sutra auto-learns the curated `docs/knowledge/` corpus into the `bugbounty` RAG collection so the
tutor can recall it and targeting gets sharper. Re-runs are cheap by construction: `RagStore.add`
de-dups by text, so unchanged docs embed nothing — running this on every startup / study tick costs
≈0 after the first pass. This deepens the COACH (knowledge it can retrieve), NOT the model's weights
— it sharpens what Sutra can teach you and where it knows bugs hide, it does not let a 7B invent
exploits. See [[project_sutra_bugbounty_money]].
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "docs" / "knowledge"
KNOWLEDGE_COLLECTION = "bugbounty"


def learn_knowledge(*, collection: str = KNOWLEDGE_COLLECTION) -> dict[str, Any]:
    """Ingest every doc under docs/knowledge/ into the knowledge collection. Idempotent + fast —
    only new/changed chunks are embedded (the store de-dups by text), so this is safe to run
    continuously. Returns {learned, total, files} (learned = chunks newly added this pass)."""
    if not KNOWLEDGE_DIR.is_dir():
        return {"learned": 0, "total": 0, "files": 0, "error": "no docs/knowledge directory"}
    from friday.rag.ingest import ingest_paths

    res = ingest_paths([str(KNOWLEDGE_DIR)], collection)
    return {"learned": res.get("chunks", 0), "total": res.get("total", 0),
            "files": res.get("files", 0)}
