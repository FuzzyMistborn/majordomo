"""
Main entry point for the Ollama Telegram bot.
"""

import logging
import os

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
# Silence noisy third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.updater").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ── Auth middleware ───────────────────────────────────────────────────────────

def _is_allowed(user_id: int) -> bool:
    if not Config.ALLOWED_USER_IDS:
        logger.warning("ALLOWED_USER_IDS is empty — all users blocked.")
        return False
    return user_id in Config.ALLOWED_USER_IDS


async def _reject(update: Update):
    await update.message.reply_text("Sorry, you're not authorised to use this bot.")


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        await _reject(update)
        return
    await update.message.reply_text(
        "Ah. You've found me.\n\n"
        "I am Wit — and before you ask, yes, I do find this line of work somewhat beneath my abilities. "
        "And yet, here we are.\n\n"
        "I can help with:\n"
        "• 📋 To-do lists _(I won't judge the contents. Much.)_\n"
        "• ⏰ Reminders _(one-off and recurring, like certain mistakes)_\n"
        "• 🔍 Web search\n"
        "• 🏠 Home Assistant control\n"
        "• 📅 Calendar events\n\n"
        "Just talk to me naturally. Use /help if you need guidance, though I'd have thought the above was self-explanatory.",
        parse_mode="Markdown"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        await _reject(update)
        return
    await update.message.reply_text(
        "*Commands, since apparently you need them:*\n\n"
        "/start — My introduction. Quite good, honestly.\n"
        "/help — This. You're looking at it.\n"
        "/clear — Make me forget our conversation. I'll pretend to be hurt.\n\n"
        "*Or just talk to me. Examples:*\n"
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


# ── Message handler ───────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        await _reject(update)
        return

    user_text = update.message.text.strip()
    if not user_text:
        return

    # Show typing indicator while processing
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    reply = await agent.chat(user_id, user_text)

    # Guard against empty reply
    if not reply or not reply.strip():
        reply = "I processed your request but had nothing to say. Please try again."

    # Render as HTML: escape special chars, convert **bold** to <b>bold</b>,
    # strip remaining model-added markdown artifacts.
    import re, html as _html
    reply = _html.escape(reply)
    reply = re.sub(r"\*\*([^*]+?)\*\*", r"<b>\1</b>", reply)
    reply = re.sub(r"\*([^*]+?)\*", r"\1", reply)
    reply = re.sub(r"_([^_]+?)_", r"\1", reply)
    reply = re.sub(r"`([^`]+?)`", r"\1", reply)

    # Telegram messages have a 4096 char limit
    for i in range(0, max(len(reply), 1), 4096):
        await update.message.reply_text(reply[i:i + 4096], parse_mode="HTML")



# ── Error handler ─────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception:", exc_info=context.error)


# ── Main ──────────────────────────────────────────────────────────────────────

async def post_init(application: Application):
    """Runs after the bot is initialised but before polling starts."""
    await db.init_db()
    logger.info("Database initialised.")

    sched.start_scheduler()
    sched.set_bot(application.bot)
    await sched.load_all_reminders()
    logger.info("Scheduler ready.")



def main():
    if not Config.TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN environment variable is not set.")

    app = (
        Application.builder()
        .token(Config.TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
