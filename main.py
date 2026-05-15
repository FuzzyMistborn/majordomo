"""
Main entry point for the Majordomo bot (Telegram + Signal).
"""

import asyncio
import html
import logging
import re
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database as db
import scheduler as sched
from ai import agent
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.updater").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ── Formatting helpers ────────────────────────────────────────────────────────

def _render_markdownish_as_html(reply: str) -> str:
    reply = html.escape(reply)
    reply = re.sub(r"\*\*([^*]+?)\*\*", r"<b>\1</b>", reply)
    reply = re.sub(r"\*([^*]+?)\*", r"\1", reply)
    reply = re.sub(r"_([^_]+?)_", r"\1", reply)
    reply = re.sub(r"`([^`]+?)`", r"\1", reply)
    return reply


def _strip_markdown(reply: str) -> str:
    reply = re.sub(r"\*\*([^*]+?)\*\*", r"\1", reply)
    reply = re.sub(r"\*([^*]+?)\*", r"\1", reply)
    reply = re.sub(r"_([^_]+?)_", r"\1", reply)
    reply = re.sub(r"`([^`]+?)`", r"\1", reply)
    return reply


# ── Telegram ──────────────────────────────────────────────────────────────────

async def _send_reply(update: Update, reply: str):
    if not reply or not reply.strip():
        reply = "I processed your request but had nothing to say. Please try again."
    reply = _render_markdownish_as_html(reply)
    for i in range(0, max(len(reply), 1), 4096):
        await update.message.reply_text(reply[i:i + 4096], parse_mode="HTML")


def _is_allowed(user_id: int) -> bool:
    if not Config.ALLOWED_USER_IDS:
        logger.warning("ALLOWED_USER_IDS is empty — all users blocked.")
        return False
    return user_id in Config.ALLOWED_USER_IDS


async def _reject(update: Update):
    await update.message.reply_text("Sorry, you're not authorised to use this bot.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        await _reject(update)
        return
    await update.message.reply_text(
        "Majordomo is ready.\n\n"
        "I can help with:\n"
        "• 📋 To-do lists\n"
        "• ⏰ Reminders, including recurring and smart reminders\n"
        "• 🔍 Web search\n"
        "• 🏠 Home Assistant control\n"
        "• 📅 Calendar events\n"
        "• 🛒 AnyList shopping lists and meal plans\n"
        "• 🎭 Switchable personalities\n\n"
        "Send a natural-language request, or use /help for examples.",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        await _reject(update)
        return
    await update.message.reply_text(
        "*Commands:*\n\n"
        "/start — Show a short overview.\n"
        "/help — Show usage examples.\n"
        "/clear — Clear your conversation history.\n\n"
        "/personality — Show current personality.\n"
        "/personality list — Show available personalities.\n"
        "/personality set plain — Switch personality.\n"
        "/reminders — List active reminders.\n"
        "/lists — List internal to-do lists and AnyList lists.\n\n"
        "*Examples:*\n"
        "• _\"Remind me to call mom tomorrow at 3pm\"_\n"
        "• _\"Add milk to my shopping list\"_\n"
        "• _\"What's on my calendar this week?\"_\n"
        "• _\"Turn off the office light\"_\n"
        "• _\"Every morning at 7am give me a smart summary of my day\"_\n"
        "• _\"Search for information on a boring new product\"_\n"
        "• _\"List personalities\"_\n"
        "• _\"Switch personality to plain\"_",
        parse_mode="Markdown",
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        await _reject(update)
        return
    agent.clear_history(update.effective_user.id)
    await update.message.reply_text("Done. I've forgotten everything. It's surprisingly easy.")


async def cmd_personality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        await _reject(update)
        return
    try:
        reply = await agent.personality_command(update.effective_user.id, context.args)
    except Exception:
        logger.exception("Error in /personality")
        reply = "Something went wrong. Please try again."
    await _send_reply(update, reply)


async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        await _reject(update)
        return
    try:
        reply = await agent.reminders_command(update.effective_user.id)
    except Exception:
        logger.exception("Error in /reminders")
        reply = "Something went wrong. Please try again."
    await _send_reply(update, reply)


async def cmd_lists(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        await _reject(update)
        return
    try:
        reply = await agent.lists_command(update.effective_user.id)
    except Exception:
        logger.exception("Error in /lists")
        reply = "Something went wrong. Please try again."
    await _send_reply(update, reply)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        await _reject(update)
        return

    user_text = update.message.text.strip()
    if not user_text:
        return
    if len(user_text) > Config.MAX_USER_MESSAGE_CHARS:
        await update.message.reply_text(
            f"Message is too long. Maximum is {Config.MAX_USER_MESSAGE_CHARS} characters."
        )
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    try:
        reply = await agent.chat(user_id, user_text)
    except Exception:
        logger.exception("Unhandled error in agent.chat()")
        reply = "Something went wrong. Please try again."

    await _send_reply(update, reply)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception:", exc_info=context.error)


# ── Signal ────────────────────────────────────────────────────────────────────

_SIGNAL_HELP = (
    "Commands: /start, /help, /clear, /reminders, /lists\n"
    "/personality — show current personality\n"
    "/personality list — show available personalities\n"
    "/personality set <name> — switch personality\n\n"
    "Examples:\n"
    "  Remind me to call mom tomorrow at 3pm\n"
    "  Add milk to my shopping list\n"
    "  What's on my calendar this week?\n"
    "  Turn off the office light"
)

_SIGNAL_START = (
    "Majordomo is ready.\n\n"
    "I can help with: to-do lists, reminders (including recurring and smart), "
    "web search, Home Assistant control, calendar events, AnyList shopping lists "
    "and meal plans, and switchable personalities.\n\n"
    "Send a natural-language request, or use /help for examples."
)


async def _handle_signal_message(sender: str, text: str) -> None:
    from services import signal as signal_svc

    user_id = await db.get_or_create_signal_user_id(sender)
    text = text.strip()
    if not text:
        return

    if text.startswith("/"):
        parts = text.split()
        cmd = parts[0].lower()
        args = parts[1:]
        if cmd == "/start":
            reply = _SIGNAL_START
        elif cmd == "/help":
            reply = _SIGNAL_HELP
        elif cmd == "/clear":
            agent.clear_history(user_id)
            reply = "Done. I've forgotten everything. It's surprisingly easy."
        elif cmd == "/personality":
            try:
                reply = await agent.personality_command(user_id, args)
            except Exception:
                logger.exception("Error in Signal /personality")
                reply = "Something went wrong. Please try again."
        elif cmd == "/reminders":
            try:
                reply = await agent.reminders_command(user_id)
            except Exception:
                logger.exception("Error in Signal /reminders")
                reply = "Something went wrong. Please try again."
        elif cmd == "/lists":
            try:
                reply = await agent.lists_command(user_id)
            except Exception:
                logger.exception("Error in Signal /lists")
                reply = "Something went wrong. Please try again."
        else:
            reply = f"Unknown command: {cmd}"
    else:
        if len(text) > Config.MAX_USER_MESSAGE_CHARS:
            reply = f"Message is too long. Maximum is {Config.MAX_USER_MESSAGE_CHARS} characters."
        else:
            try:
                reply = await agent.chat(user_id, text)
            except Exception:
                logger.exception("Unhandled error in agent.chat() [Signal]")
                reply = "Something went wrong. Please try again."

    if not reply or not reply.strip():
        reply = "I processed your request but had nothing to say. Please try again."

    reply = _strip_markdown(reply)
    await signal_svc.send_message(sender, reply)


async def signal_poll_loop() -> None:
    from services import signal as signal_svc

    logger.info("Signal polling loop started.")
    while True:
        try:
            messages = await signal_svc.receive_messages()
            for msg in messages:
                envelope = msg.get("envelope", {})
                sender = envelope.get("sourceNumber") or envelope.get("source")
                if not sender:
                    continue
                if sender not in Config.SIGNAL_ALLOWED_NUMBERS:
                    logger.warning("Blocked Signal message from %s", sender)
                    continue
                data_message = envelope.get("dataMessage") or {}
                text = data_message.get("message", "")
                if not text:
                    continue
                asyncio.create_task(_handle_signal_message(sender, text))
        except Exception:
            logger.exception("Error in Signal poll loop")
        await asyncio.sleep(2)


# ── Startup ───────────────────────────────────────────────────────────────────

def validate_startup():
    Config.validate()
    personality_dir = Path("personalities")
    if not personality_dir.exists():
        logger.warning("personalities/ directory not found; legacy personality.md fallback may be used.")


async def post_init(application: Application):
    """Runs after the Telegram bot is initialised but before polling starts."""
    await db.init_db()
    logger.info("Database initialised.")

    sched.start_scheduler()
    sched.set_bot(application.bot)
    await sched.load_all_reminders()
    logger.info("Scheduler ready.")

    if Config.SIGNAL_API_URL and Config.SIGNAL_SENDER_NUMBER:
        asyncio.create_task(signal_poll_loop())
        logger.info("Signal polling started alongside Telegram.")


async def _run_signal_only() -> None:
    """Entry point when only Signal is configured (no Telegram token)."""
    await db.init_db()
    logger.info("Database initialised.")

    sched.start_scheduler()
    await sched.load_all_reminders()
    logger.info("Scheduler ready.")

    await signal_poll_loop()


def main():
    validate_startup()

    if Config.TELEGRAM_TOKEN:
        app = (
            Application.builder()
            .token(Config.TELEGRAM_TOKEN)
            .post_init(post_init)
            .build()
        )
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("clear", cmd_clear))
        app.add_handler(CommandHandler("personality", cmd_personality))
        app.add_handler(CommandHandler("reminders", cmd_reminders))
        app.add_handler(CommandHandler("lists", cmd_lists))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        app.add_error_handler(error_handler)
        logger.info("Bot starting (Telegram%s)...", " + Signal" if Config.SIGNAL_API_URL else "")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    else:
        logger.info("Bot starting (Signal only)...")
        asyncio.run(_run_signal_only())


if __name__ == "__main__":
    main()
