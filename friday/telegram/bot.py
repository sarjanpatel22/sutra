"""Telegram bot bridge for Sutra.

Long-polling async bot. Text → agent.run() → reply. Photo → vision skill → reply.
Irreversible skill confirmations use inline Approve/Decline buttons (asyncio.Future
round-trip, mirroring friday/gui/bridge.py). Access-controlled via
TELEGRAM_ALLOWED_CHAT_IDS (comma-separated); if unset operates single-user with a
warning. Missing/invalid token → clear error, never crash.

Start via ``sutra telegram`` (never imported at package-load time).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import tempfile
from typing import Any

from friday.core.constants import GROQ_VISION_MODEL

_log = logging.getLogger("sutra.telegram")

# Per-chat history kept in RAM. Redis sync is a follow-on (noted in Phase 1.11).
_histories: dict[int, list[dict[str, Any]]] = {}
_MAX_HISTORY = 24

# Pending confirmation futures: confirm_id → Future[bool]
_pending: dict[str, asyncio.Future[bool]] = {}
_confirm_ids = itertools.count(1)
CONFIRM_TIMEOUT_S = 120.0  # default-decline after 2 min

# Callback-data prefix so we can namespace our button presses
_CB_APPROVE = "sutra:approve:"
_CB_DECLINE = "sutra:decline:"

# Vision model (free Groq) — reuses the same model as guide_me / vision_agent
_VISION_MODEL = GROQ_VISION_MODEL


def _allowed_ids() -> set[int] | None:
    """Return the set of allowed chat IDs, or None if unrestricted (log warning)."""
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not raw:
        _log.warning(
            "TELEGRAM_ALLOWED_CHAT_IDS not set — operating single-user (no access control). "
            "Set it to a comma-separated list of Telegram chat IDs to restrict access."
        )
        return None
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                ids.add(int(part))
            except ValueError:
                _log.warning("Ignoring invalid chat ID in TELEGRAM_ALLOWED_CHAT_IDS: %r", part)
    return ids or None


def _is_allowed(chat_id: int, allowed: set[int] | None) -> bool:
    return allowed is None or chat_id in allowed


async def _describe_photo(photo_bytes: bytes) -> str:
    """Send a photo to the free Groq vision model and return a description."""
    import base64

    import litellm

    b64 = base64.b64encode(photo_bytes).decode()
    try:
        r = await litellm.acompletion(
            model=_VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image concisely."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                    ],
                }
            ],
            max_tokens=300,
            temperature=0.2,
        )
        return (r.choices[0].message.content or "").strip() or "I couldn't read the image."
    except Exception as e:  # noqa: BLE001
        return f"Vision unavailable: {e!s:.120}"


async def _make_confirm(
    update: Any,
    context: Any,
) -> Any:
    """Return a confirm hook bound to this chat that sends inline buttons and awaits the
    callback, mirroring the gui/bridge.py Future round-trip."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    async def _confirm(skill: str, args: dict[str, Any]) -> bool:
        cid = str(next(_confirm_ids))
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        _pending[cid] = fut

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Approve", callback_data=f"{_CB_APPROVE}{cid}"),
                InlineKeyboardButton("Decline", callback_data=f"{_CB_DECLINE}{cid}"),
            ]
        ])
        args_preview = str(args)[:200]
        text = (
            f"Sutra wants to run an irreversible action:\n\n"
            f"*{skill}*\n`{args_preview}`\n\nApprove or Decline?"
        )
        assert update.effective_chat is not None
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        try:
            approved = await asyncio.wait_for(fut, timeout=CONFIRM_TIMEOUT_S)
        except TimeoutError:
            approved = False
            _pending.pop(cid, None)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Confirmation timed out — action declined.",
            )
        return approved

    return _confirm


async def handle_message(update: Any, context: Any) -> None:
    """Handle an incoming text message: run the agent, reply."""
    from friday.core import agent

    if update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    allowed = _allowed_ids()
    if not _is_allowed(chat_id, allowed):
        await update.message.reply_text(
            "Sorry, I'm not configured to talk to you. Contact the Sutra admin."
        )
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    history = _histories.get(chat_id, [])
    confirm = await _make_confirm(update, context)
    try:
        reply, new_history = await agent.run(text, history=history, confirm=confirm)
    except Exception as e:  # noqa: BLE001
        _log.exception("agent.run error")
        await update.message.reply_text(f"Error: {e!s:.200}")
        return

    _histories[chat_id] = new_history[-_MAX_HISTORY:]
    await update.message.reply_text(reply or "(no reply)")


async def handle_photo(update: Any, context: Any) -> None:
    """Handle a photo message: download, describe via vision model, reply."""
    if update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    allowed = _allowed_ids()
    if not _is_allowed(chat_id, allowed):
        await update.message.reply_text(
            "Sorry, I'm not configured to talk to you. Contact the Sutra admin."
        )
        return

    photos = update.message.photo
    if not photos:
        return
    # Telegram sends multiple sizes; pick the largest
    largest = max(photos, key=lambda p: p.file_size or 0)
    tmp_path = ""
    try:
        tg_file = await context.bot.get_file(largest.file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)
        with open(tmp_path, "rb") as fh:
            photo_bytes = fh.read()
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"Could not download photo: {e!s:.120}")
        return
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)  # never leak the temp file, even on a download error

    caption = (update.message.caption or "").strip()
    await update.message.reply_text("Looking at the image…")
    description = await _describe_photo(photo_bytes)
    if caption:
        reply_text = f"*Image:* {description}\n\n*Your note:* {caption}"
    else:
        reply_text = description
    await update.message.reply_text(reply_text, parse_mode="Markdown")


async def handle_callback(update: Any, context: Any) -> None:
    """Handle inline-button presses for confirm Approve/Decline."""
    from telegram import InlineKeyboardMarkup

    query = update.callback_query
    if query is None:
        return
    await query.answer()

    data = query.data or ""
    if data.startswith(_CB_APPROVE):
        cid = data[len(_CB_APPROVE):]
        approved = True
    elif data.startswith(_CB_DECLINE):
        cid = data[len(_CB_DECLINE):]
        approved = False
    else:
        return

    fut = _pending.pop(cid, None)
    if fut is not None and not fut.done():
        fut.set_result(approved)

    label = "Approved" if approved else "Declined"
    # Edit the inline keyboard away so the user sees the outcome clearly
    try:
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([]))
        await query.edit_message_text(
            text=f"{query.message.text or ''}\n\n_{label}_",
            parse_mode="Markdown",
        )
    except Exception:  # noqa: BLE001 — message may have expired
        pass


def build_application(token: str) -> Any:
    """Build the Application with all handlers registered. Separated for testability."""
    from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

    app = (
        Application.builder()
        .token(token)
        .build()
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    return app


def run_bot(token: str) -> None:
    """Start the Telegram bot with long-polling (blocking)."""
    _log.info("Starting Sutra Telegram bridge…")
    app = build_application(token)
    app.run_polling(drop_pending_updates=True)
