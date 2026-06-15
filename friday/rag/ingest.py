"""Ingest text/markdown/code files into a RagStore — paragraph-grouped chunking.

Splits on blank lines, groups paragraphs up to ~target words with a small overlap so a
passage that straddles a boundary still surfaces. Deliberately simple — good enough for
notes/docs/code, no heavyweight document parser.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from friday.rag.store import Chunk, RagStore

_EXTS = {".md", ".markdown", ".txt", ".rst", ".py"}


def chunk_text(text: str, target_words: int = 220, overlap: int = 40) -> list[str]:
    """Group paragraphs into ~target_words chunks, carrying `overlap` words across."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    cur: list[str] = []
    n = 0
    for p in paras:
        w = len(p.split())
        if n + w > target_words and cur:
            chunks.append("\n\n".join(cur))
            tail = " ".join(" ".join(cur).split()[-overlap:]) if overlap else ""
            cur = [tail] if tail else []
            n = len(tail.split())
        cur.append(p)
        n += w
    if cur and "\n\n".join(cur).strip():
        chunks.append("\n\n".join(cur))
    return [c for c in chunks if c.strip()]


def _gather(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        pp = Path(p).expanduser()
        if pp.is_dir():
            files += [f for f in sorted(pp.rglob("*"))
                      if f.is_file() and f.suffix.lower() in _EXTS]
        elif pp.is_file():
            files.append(pp)
    return files


def ingest_paths(paths: list[str], collection: str = "default") -> dict[str, Any]:
    """Chunk every supported file under `paths` and add them to the collection."""
    store = RagStore(collection)
    files = _gather(paths)
    chunks: list[Chunk] = []
    for f in files:
        try:
            text = f.read_text(errors="ignore")
        except Exception:  # noqa: BLE001 — skip unreadable files, don't abort the batch
            continue
        chunks += [Chunk(text=c, source=str(f)) for c in chunk_text(text)]
    added = store.add(chunks)
    return {"files": len(files), "chunks": added, "collection": collection, "total": len(store)}
