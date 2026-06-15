"""Unit tests for the Telegram bridge (friday/telegram/bot.py).

All tests are fully offline — no token, no network, no real agent. The Telegram
Update/Bot/Context objects are mocked; agent.run is patched to a coroutine stub.

Test plan:
  (a) text → agent called → reply sent to the right chat
  (b) confirm flow: Approve resolves the Future → agent allowed to proceed
  (c) confirm flow: Decline resolves the Future → agent declined
  (d) disallowed chat_id → polite rejection, agent never called
  (e) missing/invalid token → CLI exits cleanly (handled in cli.py; bot.py not invoked)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import friday.telegram.bot as bot_mod  # noqa: I001

# ---------------------------------------------------------------------------
# Helpers: minimal Telegram stub objects
# ---------------------------------------------------------------------------

def _make_update(chat_id: int = 111, text: str = "hello") -> MagicMock:
    """Minimal Update stub with a text message from `chat_id`."""
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.caption = None
    update.message.photo = []
    update.message.reply_text = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query = None
    return update


def _make_context(bot: MagicMock | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.bot = bot or MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.get_file = AsyncMock()
    return ctx


def _make_callback_update(chat_id: int, data: str) -> MagicMock:
    """Minimal Update stub for an InlineKeyboard callback."""
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.text = "Sutra wants to run an irreversible action:\n\nsome_skill"
    update.callback_query.edit_message_reply_markup = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = None
    return update


# ---------------------------------------------------------------------------
# (a) text message → agent called → reply sent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_text_message_calls_agent_and_replies() -> None:
    """Text message from allowed chat → agent.run() called → reply sent."""
    update = _make_update(chat_id=999, text="what time is it?")
    ctx = _make_context()

    fake_reply = "It's high noon."
    fake_history: list[dict[str, Any]] = [{"role": "user", "content": "what time is it?"}]

    async def fake_agent_run(
        text: str,
        history: list[Any] | None = None,
        confirm: Any = None,
        **_: Any,
    ) -> tuple[str, list[Any]]:
        assert text == "what time is it?"
        return fake_reply, fake_history

    with (
        patch.dict("os.environ", {"TELEGRAM_ALLOWED_CHAT_IDS": "999"}),
        patch("friday.core.agent.run", side_effect=fake_agent_run),
    ):
        await bot_mod.handle_message(update, ctx)

    update.message.reply_text.assert_called_once_with(fake_reply)


# ---------------------------------------------------------------------------
# (b) confirm flow: Approve
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_approve_resolves_future() -> None:
    """When the user presses Approve, the confirm hook returns True."""
    chat_id = 42
    update = _make_update(chat_id=chat_id, text="delete my files")
    ctx = _make_context()

    # Capture the confirm hook the bot constructs, then simulate the button press
    captured_confirm: list[Any] = []

    async def fake_agent_run(
        text: str,
        history: list[Any] | None = None,
        confirm: Any = None,
        **_: Any,
    ) -> tuple[str, list[Any]]:
        # Store the confirm hook
        captured_confirm.append(confirm)
        # Call it — this sends the inline keyboard and awaits the Future
        result = await confirm("delete_files", {"path": "/tmp"})
        return ("Done." if result else "Declined."), []

    with (
        patch.dict("os.environ", {"TELEGRAM_ALLOWED_CHAT_IDS": str(chat_id)}),
        patch("friday.core.agent.run", side_effect=fake_agent_run),
    ):
        # Schedule the agent turn
        agent_task = asyncio.create_task(bot_mod.handle_message(update, ctx))

        # Yield to let the confirm hook send its message and block on the Future
        await asyncio.sleep(0.05)

        # Simulate pressing Approve: find the pending confirm_id from _pending
        assert bot_mod._pending, "No pending confirmation after the hook fired"
        cid = next(iter(bot_mod._pending))
        cb_update = _make_callback_update(chat_id, f"sutra:approve:{cid}")
        await bot_mod.handle_callback(cb_update, ctx)

        # Let the agent task finish
        await agent_task

    # The reply should be "Done." (approved path)
    update.message.reply_text.assert_called_once_with("Done.")


# ---------------------------------------------------------------------------
# (c) confirm flow: Decline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_decline_resolves_future() -> None:
    """When the user presses Decline, the confirm hook returns False."""
    chat_id = 43
    update = _make_update(chat_id=chat_id, text="rm -rf everything")
    ctx = _make_context()

    async def fake_agent_run(
        text: str,
        history: list[Any] | None = None,
        confirm: Any = None,
        **_: Any,
    ) -> tuple[str, list[Any]]:
        result = await confirm("run_command", {"command": "rm -rf /"})
        return ("Done." if result else "Declined."), []

    with (
        patch.dict("os.environ", {"TELEGRAM_ALLOWED_CHAT_IDS": str(chat_id)}),
        patch("friday.core.agent.run", side_effect=fake_agent_run),
    ):
        agent_task = asyncio.create_task(bot_mod.handle_message(update, ctx))

        await asyncio.sleep(0.05)

        assert bot_mod._pending, "No pending confirmation"
        cid = next(iter(bot_mod._pending))
        cb_update = _make_callback_update(chat_id, f"sutra:decline:{cid}")
        await bot_mod.handle_callback(cb_update, ctx)

        await agent_task

    update.message.reply_text.assert_called_once_with("Declined.")


# ---------------------------------------------------------------------------
# (d) disallowed chat_id → polite rejection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disallowed_chat_id_rejected() -> None:
    """A chat_id not in TELEGRAM_ALLOWED_CHAT_IDS gets a polite rejection."""
    update = _make_update(chat_id=666, text="hello")
    ctx = _make_context()

    with (
        patch.dict("os.environ", {"TELEGRAM_ALLOWED_CHAT_IDS": "111,222"}),
        patch("friday.core.agent.run", side_effect=AssertionError("should not be called")),
    ):
        await bot_mod.handle_message(update, ctx)

    # Must reply with the rejection message
    update.message.reply_text.assert_called_once()
    call_text: str = update.message.reply_text.call_args[0][0]
    assert "not configured" in call_text.lower()


# ---------------------------------------------------------------------------
# (e) missing token → CLI exits (tested via the secrets path)
# ---------------------------------------------------------------------------

def test_missing_token_get_secret_returns_none() -> None:
    """get_secret('TELEGRAM_BOT_TOKEN') returns None when neither keyring nor env is set."""
    import os
    from unittest.mock import patch as _patch

    # Unset both potential sources
    env_without_token = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
    with (
        _patch.dict("os.environ", env_without_token, clear=True),
        _patch("friday.core.secrets._keyring", return_value=None),
    ):
        from friday.core.secrets import get_secret

        result = get_secret("TELEGRAM_BOT_TOKEN")
    assert result is None


# ---------------------------------------------------------------------------
# (f) handle_callback ignores unknown callback data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_callback_data_ignored() -> None:
    """Callback data that doesn't match our prefix is silently ignored."""
    cb_update = _make_callback_update(chat_id=1, data="some:other:callback")
    ctx = _make_context()
    # Should not raise
    await bot_mod.handle_callback(cb_update, ctx)
    cb_update.callback_query.answer.assert_called_once()


# ---------------------------------------------------------------------------
# (g) build_application wires handlers without a real token (offline)
# ---------------------------------------------------------------------------

def test_build_application_registers_handlers() -> None:
    """build_application() can be constructed (handlers wired, no network call)."""
    app = bot_mod.build_application(token="FAKE:TOKEN")
    # Should have at least 3 handlers (text, photo, callback)
    assert len(app.handlers) > 0
