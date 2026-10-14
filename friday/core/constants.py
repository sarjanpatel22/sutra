"""Shared model-id constants — one source of truth so a provider swap is a
one-line change, not a multi-file hunt.

The free vision model sits behind every vision path (guide_me, sutra watch, the
screen-viewer web agent, the Telegram photo handler). Groq deprecated its LAST
vision model (llama-4-scout, decommissioned 2026-07-17 — see
docs/MODEL_REFRESH_2026-06.md), so vision moved to Gemini's free multimodal model
(already wired into the router and key present). Override with VISION_MODEL (or the
legacy GROQ_VISION_MODEL) if it changes again. A $0 LOCAL fallback also exists:
`ollama_chat/gemma3:4b` is multimodal.
"""

from __future__ import annotations

import os

# Free multimodal model behind every vision path. Gemini Flash (free tier) replaced Groq's
# retired llama-4-scout. Override via VISION_MODEL; GROQ_VISION_MODEL kept as a legacy alias.
VISION_MODEL = (
    os.getenv("VISION_MODEL") or os.getenv("GROQ_VISION_MODEL") or "gemini/gemini-2.5-flash"
)
GROQ_VISION_MODEL = VISION_MODEL  # legacy alias — still imported by the vision skills
