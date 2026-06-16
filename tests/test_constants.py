"""Regression lock: the Groq vision model id is single-sourced, not copy-pasted.

Groq retires vision models (llama-4-maverick was decommissioned 2026), so every
vision path must read the one shared constant — otherwise a future swap silently
misses a file and that path breaks. This test fails the moment a private copy
diverges from friday.core.constants.GROQ_VISION_MODEL.
"""

from friday.core.constants import GROQ_VISION_MODEL


def test_vision_model_is_single_sourced():
    from friday.skills import guide, vision_agent
    from friday.telegram import bot
    from friday.watch import state, suggest

    assert guide._VISION_MODEL == GROQ_VISION_MODEL
    assert vision_agent._VISION_MODEL == GROQ_VISION_MODEL
    assert bot._VISION_MODEL == GROQ_VISION_MODEL
    assert state._DEFAULT_MODEL == GROQ_VISION_MODEL
    assert suggest.DEFAULT_MODEL == GROQ_VISION_MODEL
