import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _parse_allowed_user_ids(raw: str) -> tuple[list[int], list[str]]:
    user_ids: list[int] = []
    invalid: list[str] = []
    for uid in raw.split(","):
        uid = uid.strip()
        if not uid:
            continue
        try:
            user_ids.append(int(uid))
        except ValueError:
            invalid.append(uid)
    return user_ids, invalid


def _parse_signal_user_map(raw: str) -> tuple[dict[str, int], list[str]]:
    """Parse SIGNAL_USER_MAP entries of the form '+phone:telegram_id,...'."""
    mapping: dict[str, int] = {}
    invalid: list[str] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.rsplit(":", 1)
        if len(parts) != 2:
            invalid.append(entry)
            continue
        phone, tid = parts[0].strip(), parts[1].strip()
        try:
            mapping[phone] = int(tid)
        except ValueError:
            invalid.append(entry)
    return mapping, invalid


_INVALID_INT_SETTINGS: list[str] = []


def _parse_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        _INVALID_INT_SETTINGS.append(name)
        return default

class Config:
    # Telegram
    TELEGRAM_TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "")
    ALLOWED_USER_IDS, INVALID_ALLOWED_USER_IDS = _parse_allowed_user_ids(
        os.environ.get("ALLOWED_USER_IDS", "")
    )

    # Signal
    SIGNAL_API_URL: str = os.environ.get("SIGNAL_API_URL", "")
    SIGNAL_SENDER_NUMBER: str = os.environ.get("SIGNAL_SENDER_NUMBER", "")
    SIGNAL_ALLOWED_NUMBERS: list[str] = [
        n.strip()
        for n in os.environ.get("SIGNAL_ALLOWED_NUMBERS", "").split(",")
        if n.strip()
    ]
    SIGNAL_USER_MAP, INVALID_SIGNAL_USER_MAP = _parse_signal_user_map(
        os.environ.get("SIGNAL_USER_MAP", "")
    )

    # Tavily
    TAVILY_API_KEY: str = os.environ.get("TAVILY_API_KEY", "")

    # llama.cpp
    LLAMACPP_HOST: str = os.environ.get("LLAMACPP_HOST", "http://host.docker.internal:8080/v1/")
    LLAMACPP_MODEL: str = os.environ.get("LLAMACPP_MODEL", "gemma-4-e4b")

    # Home Assistant
    HA_URL: str = os.environ.get("HA_URL", "")
    HA_TOKEN: str = os.environ.get("HA_TOKEN", "")
    HA_ALLOWED_DOMAINS: list[str] = [
        d.strip()
        for d in os.environ.get("HA_ALLOWED_DOMAINS", "light,switch,input_boolean,script,automation,climate,cover,fan,media_player").split(",")
        if d.strip()
    ]

    HA_WEATHER_ENTITY: str = os.environ.get("HA_WEATHER_ENTITY", "")

    # CalDAV
    CALDAV_URL: str = os.environ.get("CALDAV_URL", "")
    CALDAV_USERNAME: str = os.environ.get("CALDAV_USERNAME", "")
    CALDAV_PASSWORD: str = os.environ.get("CALDAV_PASSWORD", "")
    CALDAV_CALENDARS: list[str] = [
        c.strip()
        for c in os.environ.get("CALDAV_CALENDARS", "").split(",")
        if c.strip()
    ]

    # AnyList
    ANYLIST_EMAIL: str = os.environ.get("ANYLIST_EMAIL", "")
    ANYLIST_PASSWORD: str = os.environ.get("ANYLIST_PASSWORD", "")
    ANYLIST_ICAL_URL: str = os.environ.get("ANYLIST_ICAL_URL", "")

    # Database
    DB_PATH: str = os.environ.get("DB_PATH", "/data/bot.db")

    # Timezone
    TIMEZONE: str = os.environ.get("TIMEZONE", "UTC")

    # Conversation history window
    HISTORY_WINDOW: int = _parse_int_env("HISTORY_WINDOW", 20)

    # Bounds
    MAX_USER_MESSAGE_CHARS: int = _parse_int_env("MAX_USER_MESSAGE_CHARS", 4000)
    MAX_LIST_NAME_CHARS: int = _parse_int_env("MAX_LIST_NAME_CHARS", 80)
    MAX_TODO_ITEM_CHARS: int = _parse_int_env("MAX_TODO_ITEM_CHARS", 2000)
    MAX_REMINDER_MESSAGE_CHARS: int = _parse_int_env("MAX_REMINDER_MESSAGE_CHARS", 1000)
    MAX_MEMORY_KEY_CHARS: int = _parse_int_env("MAX_MEMORY_KEY_CHARS", 80)
    MAX_MEMORY_VALUE_CHARS: int = _parse_int_env("MAX_MEMORY_VALUE_CHARS", 2000)
    MAX_SEARCH_QUERY_CHARS: int = _parse_int_env("MAX_SEARCH_QUERY_CHARS", 500)
    MAX_NOTE_TITLE_CHARS: int = _parse_int_env("MAX_NOTE_TITLE_CHARS", 200)
    MAX_NOTE_CONTENT_CHARS: int = _parse_int_env("MAX_NOTE_CONTENT_CHARS", 10000)
    MAX_SETTING_KEY_CHARS: int = _parse_int_env("MAX_SETTING_KEY_CHARS", 80)
    MAX_SETTING_VALUE_CHARS: int = _parse_int_env("MAX_SETTING_VALUE_CHARS", 2000)

    # Integration timeout for sync libraries wrapped in executors.
    INTEGRATION_TIMEOUT_SECONDS: int = _parse_int_env("INTEGRATION_TIMEOUT_SECONDS", 20)

    @classmethod
    def validate(cls) -> None:
        errors: list[str] = []
        telegram_enabled = bool(cls.TELEGRAM_TOKEN)
        signal_enabled = bool(cls.SIGNAL_API_URL and cls.SIGNAL_SENDER_NUMBER)
        if not telegram_enabled and not signal_enabled:
            errors.append(
                "At least one platform must be configured: set TELEGRAM_TOKEN, "
                "or set both SIGNAL_API_URL and SIGNAL_SENDER_NUMBER."
            )
        if telegram_enabled and not cls.ALLOWED_USER_IDS:
            errors.append("ALLOWED_USER_IDS must contain at least one Telegram user ID when Telegram is enabled.")
        if cls.INVALID_ALLOWED_USER_IDS:
            errors.append(
                "ALLOWED_USER_IDS contains invalid values: "
                + ", ".join(cls.INVALID_ALLOWED_USER_IDS)
            )
        if cls.INVALID_SIGNAL_USER_MAP:
            errors.append(
                "SIGNAL_USER_MAP contains invalid entries (expected +phone:telegram_id): "
                + ", ".join(cls.INVALID_SIGNAL_USER_MAP)
            )
        if signal_enabled and not cls.SIGNAL_ALLOWED_NUMBERS:
            errors.append("SIGNAL_ALLOWED_NUMBERS must contain at least one phone number when Signal is enabled.")
        if bool(cls.SIGNAL_API_URL) != bool(cls.SIGNAL_SENDER_NUMBER):
            errors.append("SIGNAL_API_URL and SIGNAL_SENDER_NUMBER must both be set together.")
        if not cls.TAVILY_API_KEY:
            errors.append("TAVILY_API_KEY is required.")
        if _INVALID_INT_SETTINGS:
            errors.append("Invalid integer environment values: " + ", ".join(_INVALID_INT_SETTINGS))
        try:
            ZoneInfo(cls.TIMEZONE)
        except ZoneInfoNotFoundError:
            errors.append(f"TIMEZONE is not a valid IANA timezone: {cls.TIMEZONE}")
        db_parent = Path(cls.DB_PATH).expanduser().parent
        if not db_parent.exists():
            errors.append(f"DB_PATH parent directory does not exist: {db_parent}")
        elif not os.access(db_parent, os.W_OK):
            errors.append(f"DB_PATH parent directory is not writable: {db_parent}")
        if cls.HA_URL and not cls.HA_TOKEN:
            errors.append("HA_TOKEN is required when HA_URL is set.")
        if cls.HA_TOKEN and not cls.HA_URL:
            errors.append("HA_URL is required when HA_TOKEN is set.")
        caldav_any = bool(cls.CALDAV_URL or cls.CALDAV_USERNAME or cls.CALDAV_PASSWORD)
        caldav_all = bool(cls.CALDAV_URL and cls.CALDAV_USERNAME and cls.CALDAV_PASSWORD)
        if caldav_any and not caldav_all:
            errors.append("CALDAV_URL, CALDAV_USERNAME, and CALDAV_PASSWORD must be set together.")
        anylist_any = bool(cls.ANYLIST_EMAIL or cls.ANYLIST_PASSWORD)
        anylist_all = bool(cls.ANYLIST_EMAIL and cls.ANYLIST_PASSWORD)
        if anylist_any and not anylist_all:
            errors.append("ANYLIST_EMAIL and ANYLIST_PASSWORD must be set together.")
        if cls.INTEGRATION_TIMEOUT_SECONDS <= 0:
            errors.append("INTEGRATION_TIMEOUT_SECONDS must be greater than zero.")
        positive_limits = {
            "HISTORY_WINDOW": cls.HISTORY_WINDOW,
            "MAX_USER_MESSAGE_CHARS": cls.MAX_USER_MESSAGE_CHARS,
            "MAX_LIST_NAME_CHARS": cls.MAX_LIST_NAME_CHARS,
            "MAX_TODO_ITEM_CHARS": cls.MAX_TODO_ITEM_CHARS,
            "MAX_REMINDER_MESSAGE_CHARS": cls.MAX_REMINDER_MESSAGE_CHARS,
            "MAX_MEMORY_KEY_CHARS": cls.MAX_MEMORY_KEY_CHARS,
            "MAX_MEMORY_VALUE_CHARS": cls.MAX_MEMORY_VALUE_CHARS,
            "MAX_SEARCH_QUERY_CHARS": cls.MAX_SEARCH_QUERY_CHARS,
            "MAX_NOTE_TITLE_CHARS": cls.MAX_NOTE_TITLE_CHARS,
            "MAX_NOTE_CONTENT_CHARS": cls.MAX_NOTE_CONTENT_CHARS,
            "MAX_SETTING_KEY_CHARS": cls.MAX_SETTING_KEY_CHARS,
            "MAX_SETTING_VALUE_CHARS": cls.MAX_SETTING_VALUE_CHARS,
        }
        invalid_limits = [name for name, value in positive_limits.items() if value <= 0]
        if invalid_limits:
            errors.append("These numeric settings must be greater than zero: " + ", ".join(invalid_limits))
        if errors:
            raise RuntimeError("Invalid configuration:\n- " + "\n- ".join(errors))
