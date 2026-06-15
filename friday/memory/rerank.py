"""LLM reranker over recall candidates — sharpens the bi-encoder's top-N into a better top-k.

The prefixed-nomic index reliably gets the RIGHT facts into the top-N (hit@5 = 100%), but a
bi-encoder can mis-order close pairs — e.g. for "who does Sarjan work for?" it scores "based
in Toronto" (0.824) a hair above "engineer at Acme Corp" (0.813). A cross-encoder-style pass
reads the question and each candidate TOGETHER and reorders by true relevance.

Free (Groq via litellm), one call over the small candidate set, and it degrades to the input
order on any failure — so reranking can only help, never drop facts or crash recall. Off the
hot path by default (it adds ~300-500ms); enable with RECALL_RERANK=1, and the memory GUI
uses it for its scored view.
"""

from __future__ import annotations

import os
import re

_MODEL = os.getenv("RECALL_RERANK_MODEL", "groq/llama-3.3-70b-versatile")

_SYS = (
    "You rank facts by how directly they ANSWER the user's question. Output ONLY the "
    "fact indices, best first, comma-separated (e.g. '3,0,1'). List only genuinely "
    "relevant facts; omit irrelevant ones."
)


def rerank_enabled() -> bool:
    """Whether the LLM rerank pass is switched on (off by default — it costs latency)."""
    return os.getenv("RECALL_RERANK", "0").strip().lower() not in ("", "0", "false", "no")


def rerank(query: str, candidates: list[str], k: int = 5) -> list[str]:
    """Reorder `candidates` by relevance to `query`; return the top-k.

    Falls back to the input order (truncated to k) on ANY failure — no key, rate limit,
    junk reply — so recall is never worse than the bi-encoder ranking, only sharper.
    """
    query = query.strip()
    if len(candidates) <= 1 or not query:
        return candidates[:k]
    try:
        import litellm

        listing = "\n".join(f"{i}: {c}" for i, c in enumerate(candidates))
        r = litellm.completion(
            model=_MODEL,
            messages=[{"role": "system", "content": _SYS},
                      {"role": "user", "content": f"Question: {query}\n\nFacts:\n{listing}"}],
            temperature=0.0, max_tokens=60,
        )
        order = [int(x) for x in re.findall(r"\d+", r.choices[0].message.content or "")]
    except Exception:  # noqa: BLE001 — any failure → keep the bi-encoder order
        return candidates[:k]

    seen: set[int] = set()
    ranked = [candidates[i] for i in order
              if 0 <= i < len(candidates) and not (i in seen or seen.add(i))]
    # Append anything the model didn't mention, in original order — never drop a fact.
    ranked += [c for j, c in enumerate(candidates) if j not in seen]
    return ranked[:k]
