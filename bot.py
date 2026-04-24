"""Telegram entry point.

Run:
    cp .env.example .env  # fill in
    pip install -r requirements.txt
    python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os

# Must run BEFORE importing agent/tools — those modules read env vars at import.
from dotenv import load_dotenv
load_dotenv()

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent import Conversation, quick_ack

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
log = logging.getLogger("bot")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED = {
    int(x) for x in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if x.strip()
}

# One conversation per chat_id, kept in memory for the process lifetime.
CONVERSATIONS: dict[int, Conversation] = {}


def _conv(chat_id: int) -> Conversation:
    if chat_id not in CONVERSATIONS:
        CONVERSATIONS[chat_id] = Conversation()
    return CONVERSATIONS[chat_id]


def _allowed(update: Update) -> bool:
    if not ALLOWED:
        return True  # if unset, allow all (dev mode)
    return update.effective_chat and update.effective_chat.id in ALLOWED


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        log.warning("denied chat_id=%s", update.effective_chat.id)
        return
    await update.message.reply_text(
        "hey 👋  ask me for a dinner rez. e.g.\n"
        "_\"dinner tnt for 2 at 8 in the mission, lively\"_\n\n"
        "/reset to clear conversation, /id to see your chat id",
        parse_mode=ParseMode.MARKDOWN,
    )


async def reset(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    _conv(update.effective_chat.id).reset()
    await update.message.reply_text("cleared.")


async def chat_id(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"chat_id: `{update.effective_chat.id}`",
                                    parse_mode=ParseMode.MARKDOWN)


async def _keep_typing(chat, stop: asyncio.Event) -> None:
    """Re-send the typing action every 4s so the indicator stays live."""
    while not stop.is_set():
        try:
            await chat.send_action(ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


async def _delayed_ack(message, user_text: str, stop: asyncio.Event,
                       delay: float = 3.0) -> None:
    """Send a context-aware ack (Haiku-generated) only if the work hasn't
    finished by `delay`s. Quick conversational replies stay clean; long
    searches get a useful heads-up like 'Pulling SoHo dinner options for 8pm…'.
    """
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except asyncio.TimeoutError:
        # Still working — generate + send ack. Done in a thread so we don't
        # block the event loop on the Haiku call.
        try:
            ack = await asyncio.to_thread(quick_ack, user_text)
            # Race check: agent might have finished while Haiku was generating.
            if not stop.is_set():
                await message.reply_text(ack)
        except Exception:
            pass


async def handle(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        log.warning("denied chat_id=%s text=%r",
                    update.effective_chat.id, update.message.text)
        return

    chat = update.effective_chat
    text = update.message.text or ""
    log.info("[%s] %s", chat.id, text)

    # Typing indicator immediately; "Checking that..." text only if it ends up
    # being a slow request (search). Conversational replies stay clean.
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(chat, stop))
    ack_task = asyncio.create_task(_delayed_ack(update.message, text, stop, delay=3.0))

    try:
        # The agent is sync + network-bound; offload to a thread so the bot's
        # event loop stays responsive (typing pings, other chats).
        reply = await asyncio.to_thread(_conv(chat.id).send, text)
    except Exception as e:
        log.exception("agent failed")
        reply = f"⚠️ {type(e).__name__}: {e}"
    finally:
        stop.set()
        await typing_task
        await ack_task

    # Telegram has a 4096-char message limit; chunk if needed.
    for chunk in _chunks(reply, 3500):
        await update.message.reply_text(
            chunk,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )


def _chunks(s: str, n: int):
    for i in range(0, len(s), n):
        yield s[i:i + n]


def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("id", chat_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    log.info("bot up; allowed chats=%s", ALLOWED or "ALL")
    app.run_polling()


if __name__ == "__main__":
    main()
