"""Shared model-id constants — one source of truth so a provider rename is a
one-line change, not a multi-file hunt.

The free Groq multimodal model sits behind every vision path (guide_me, sutra
watch, the screen-viewer web agent, the Telegram photo handler). Verified active
2026-06-15 — Groq decommissioned llama-4-maverick but NOT scout (see
docs/MODEL_REFRESH_2026-06.md), which is exactly why the id belongs in one place:
when Groq eventually retires it, this is the only line (or the env var) to change.
"""

from __future__ import annotations

import os

# Free Groq vision model. Override with GROQ_VISION_MODEL if Groq ever retires it.
GROQ_VISION_MODEL = os.getenv(
    "GROQ_VISION_MODEL", "groq/meta-llama/llama-4-scout-17b-16e-instruct"
)
