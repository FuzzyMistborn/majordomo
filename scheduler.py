import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import database as db
from config import Config
from ctx import firing_reminder_id

logger = logging.getLogger(__name__)

# Set by main.py after the Telegram bot is initialised (None when Signal-only)
_bot = None
_scheduler: AsyncIOScheduler | None = None

# user_id -> reminder_id of the most recently fired reminder (in-memory, lost on restart)
_last_fired: dict[int, int] = {}
_LAST_FIRED_SETTING_KEY = "last_fired_reminder_id"


async def get_last_fired_reminder_id_persistent(user_id: int) -> int | None:
    if user_id in _last_fired:
        return _last_fired[user_id]
    saved = await db.get_user_setting(user_id, _LAST_FIRED_SETTING_KEY)
    if not saved:
        return None
    try:
        reminder_id = int(saved)
    except ValueError:
        return None
    _last_fired[user_id] = reminder_id
    return reminder_id


async def _set_last_fired_reminder(user_id: int, reminder_id: int) -> None:
    _last_fired[user_id] = reminder_id
    await db.save_user_setting(user_id, _LAST_FIRED_SETTING_KEY, str(reminder_id))


def set_bot(bot):
    global _bot
    _bot = bot


def get_scheduler() -> AsyncIOScheduler:
    return _scheduler


async def _send_message(user_id: int, text: str):
    phone = await db.get_signal_phone_by_user_id(user_id)
    if phone:
        from services import signal as signal_svc
        await signal_svc.send_message(phone, text)
        return
    if _bot is None:
        logger.error("Bot not set in scheduler, cannot send message to user %s.", user_id)
        return
    try:
        for i in range(0, max(len(text), 1), 4096):
            await _bot.send_message(chat_id=user_id, text=text[i:i + 4096])
    except Exception as e:
        logger.error(f"Failed to send Telegram message to {user_id}: {e}")


async def _run_smart_reminder(reminder_id: int, user_id: int, instruction: str):
    """
    Run a smart reminder: call the AI agent with the instruction and send the result.
    Imports agent here to avoid circular imports.
    """
    try:
        from ai.agent import chat
        logger.info(f"Running smart reminder {reminder_id} for user {user_id}: {instruction[:80]}")
        token = firing_reminder_id.set(reminder_id)
        try:
            result = await chat(user_id, instruction, smart_reminder=True)
        finally:
            firing_reminder_id.reset(token)
        await _send_message(user_id, f"🌅 Morning Briefing:\n\n{result}")
    except Exception as e:
        logger.error(f"Smart reminder {reminder_id} failed: {e}", exc_info=True)
        await _send_message(user_id, f"⏰ Reminder:\n{instruction}\n\nCould not generate dynamic summary: {e}")


async def _fire_reminder(reminder_id: int, user_id: int, message: str, original_time: str, late: bool = False):
    prefix = f"⏰ Reminder (originally scheduled for {original_time}):\n" if late else "⏰ Reminder:\n"
    await _send_message(user_id, f"{prefix}{message}")


async def _one_shot_job(reminder_id: int, user_id: int, message: str, original_time: str, late: bool = False, smart: bool = False):
    await _set_last_fired_reminder(user_id, reminder_id)
    if smart:
        await _run_smart_reminder(reminder_id, user_id, message)
    else:
        await _fire_reminder(reminder_id, user_id, message, original_time, late)
    await db.mark_reminder_fired(reminder_id)


async def _recurring_job(reminder_id: int, user_id: int, message: str, fire_at: str, smart: bool = False):
    await _set_last_fired_reminder(user_id, reminder_id)
    if smart:
        await _run_smart_reminder(reminder_id, user_id, message)
    else:
        await _send_message(user_id, f"⏰ Reminder:\n{message}")


def _parse_recurrence_to_cron(recurrence_json: str) -> CronTrigger | None:
    try:
        data = json.loads(recurrence_json)
        tz = data.pop("timezone", Config.TIMEZONE)
        return CronTrigger(**data, timezone=tz)
    except Exception as e:
        logger.error(f"Failed to parse recurrence '{recurrence_json}': {e}")
        return None


async def schedule_reminder(reminder: dict) -> bool:
    scheduler = get_scheduler()
    if scheduler is None:
        return False

    job_id = f"reminder_{reminder['id']}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    fire_at_str: str = reminder["fire_at"]
    smart: bool = bool(reminder.get("smart", 0))

    try:
        fire_at = datetime.fromisoformat(fire_at_str)
    except ValueError:
        logger.error(f"Invalid fire_at format for reminder {reminder['id']}: {fire_at_str}")
        return False

    now = datetime.now(timezone.utc)
    if fire_at.tzinfo is None:
        fire_at = fire_at.replace(tzinfo=ZoneInfo(Config.TIMEZONE))

    if reminder["recurrence"]:
        trigger = _parse_recurrence_to_cron(reminder["recurrence"])
        if trigger is None:
            return False
        scheduler.add_job(
            _recurring_job,
            trigger=trigger,
            id=job_id,
            kwargs={
                "reminder_id": reminder["id"],
                "user_id": reminder["user_id"],
                "message": reminder["message"],
                "fire_at": fire_at_str,
                "smart": smart,
            },
            replace_existing=True,
        )
    else:
        late = fire_at < now
        scheduler.add_job(
            _one_shot_job,
            trigger=DateTrigger(run_date=now if late else fire_at),
            id=job_id,
            kwargs={
                "reminder_id": reminder["id"],
                "user_id": reminder["user_id"],
                "message": reminder["message"],
                "original_time": fire_at_str,
                "late": late,
                "smart": smart,
            },
            replace_existing=True,
        )

    return True


async def load_all_reminders():
    reminders = await db.get_all_active_reminders()
    logger.info(f"Loading {len(reminders)} active reminder(s) from DB.")
    for reminder in reminders:
        await schedule_reminder(reminder)


async def unschedule_reminder(reminder_id: int):
    scheduler = get_scheduler()
    job_id = f"reminder_{reminder_id}"
    if scheduler and scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def start_scheduler():
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone=Config.TIMEZONE)
    _scheduler.start()
    logger.info("Scheduler started.")
    return _scheduler
