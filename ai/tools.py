"""
Tool definitions and handlers for the Ollama AI agent.
"""

import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import database as db
import scheduler as sched
from config import Config
from services import anylist as anylist_service
from services import calendar as cal_service
from services import homeassistant as ha
from services import search as search_service

logger = logging.getLogger(__name__)


def _normalize_list_name(name: str) -> str:
    """Strip trailing 'list'/'lists' that models often append."""
    import re
    return re.sub(r"\s+lists?$", "", name, flags=re.IGNORECASE).strip()


def _normalize_reminder_message(msg: str) -> str:
    import re
    return re.sub(r"^(?:remind(?:er)?(?:\s+me)?(?:\s+to)?[\s:]+)", "", msg, flags=re.IGNORECASE).strip()


def _parse_snooze_duration(s: str) -> int | None:
    """Parse a human duration string into minutes. Returns None if unparseable."""
    import re
    s = s.lower().strip()
    patterns = [
        (r'(\d+)\s*(?:days?|d\b)', 24 * 60),
        (r'(\d+)\s*(?:hours?|hrs?|h\b)', 60),
        (r'(\d+)\s*(?:minutes?|mins?|m\b)(?!onths?)', 1),
    ]
    total = 0
    matched = False
    for pattern, mult in patterns:
        for m in re.finditer(pattern, s):
            total += int(m.group(1)) * mult
            matched = True
    return int(total) if matched and total > 0 else None


def _now_local() -> datetime:
    return datetime.now(ZoneInfo(Config.TIMEZONE))


def _fix_year(dt_str: str | None) -> str | None:
    """Replace a hallucinated year with the current year."""
    if not dt_str or len(dt_str) < 4:
        return dt_str
    current_year = _now_local().year
    try:
        if int(dt_str[:4]) != current_year:
            logger.warning(f"Correcting hallucinated year in datetime: {dt_str}")
            return str(current_year) + dt_str[4:]
    except ValueError:
        pass
    return dt_str


def _parse_datetime(dt_str: str) -> datetime:
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(Config.TIMEZONE))
    return dt


TOOL_DEFINITIONS = [
    # ── Todo ──
    {
        "type": "function",
        "function": {
            "name": "todo_create_list",
            "description": "Create a new named to-do list.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_delete_list",
            "description": "Delete a to-do list and all its items.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_get_lists",
            "description": "Get all to-do lists.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_add_item",
            "description": "Add an item to a named to-do list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "list_name": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["list_name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_get_items",
            "description": "Get all items in a named to-do list.",
            "parameters": {"type": "object", "properties": {"list_name": {"type": "string"}}, "required": ["list_name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_delete_item",
            "description": "Delete a specific to-do item by ID.",
            "parameters": {"type": "object", "properties": {"item_id": {"type": "integer"}}, "required": ["item_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_clear_list",
            "description": "Remove all items from a named to-do list (keeps the list itself).",
            "parameters": {"type": "object", "properties": {"list_name": {"type": "string"}}, "required": ["list_name"]},
        },
    },
    # ── Reminders ──
    {
        "type": "function",
        "function": {
            "name": "reminder_create",
            "description": (
                "Create a reminder. fire_at is ISO 8601. "
                "For recurring, also provide recurrence as JSON cron spec "
                "(e.g. {\"minute\":\"0\",\"hour\":\"9\",\"day_of_week\":\"mon\"}) "
                "and recurrence_human as a readable description. "
                "Set smart=true to make the reminder dynamically fetch calendar events and reminders at fire time instead of sending a static message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "The reminder text, or an instruction for smart reminders e.g. \'Give me a summary of today\'s calendar events and any reminders I have\'"},
                    "fire_at": {"type": "string"},
                    "recurrence": {"type": "string"},
                    "recurrence_human": {"type": "string"},
                    "smart": {"type": "boolean", "description": "If true, the message is an AI instruction run at fire time rather than a static reminder text"},
                },
                "required": ["message", "fire_at"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_list",
            "description": "List all active reminders.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_delete",
            "description": "Delete a reminder by ID.",
            "parameters": {"type": "object", "properties": {"reminder_id": {"type": "integer"}}, "required": ["reminder_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_snooze",
            "description": (
                "Snooze a reminder — reschedule it to fire again after a delay. "
                "Call with just duration (e.g. '10 minutes') to snooze the most recently fired reminder. "
                "If duration is omitted, ask the user how long to snooze. "
                "Optionally provide reminder_id to snooze a specific reminder by ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "duration": {"type": "string", "description": "How long to snooze, e.g. '10 minutes', '1 hour'. Omit to ask."},
                    "reminder_id": {"type": "integer", "description": "ID of the reminder to snooze. Defaults to the most recently fired one."},
                },
                "required": [],
            },
        },
    },
    # ── Memory ──
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "CALL THIS TOOL when the user says 'remember', 'note that', 'save that', or provides a reusable fact (e.g. a calendar name, HA entity, person's name). Do NOT just say you'll remember — call this tool. Overwrites any existing value for the same key.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Short label for the fact (e.g. \"wife's calendar\", \"office light\")"},
                    "value": {"type": "string", "description": "The actual value to remember (e.g. \"Family Vacations\", \"light.office_main\")"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_delete",
            "description": "Forget a previously saved fact by its key.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_list",
            "description": "List all saved facts for this user.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    # ── Weather ──
    {
        "type": "function",
        "function": {
            "name": "ha_get_weather",
            "description": "Get current weather conditions and forecast from Home Assistant.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    # ── Web Search ──
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web. Returns a short summary and top links.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    },
    # ── Home Assistant ──
    {
        "type": "function",
        "function": {
            "name": "ha_turn_on",
            "description": "Turn on a Home Assistant entity. Provide the exact entity_id (e.g. light.office, switch.kitchen).",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string", "description": "Exact entity_id"}},
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ha_turn_off",
            "description": "Turn off a Home Assistant entity. Provide the exact entity_id.",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string", "description": "Exact entity_id"}},
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ha_toggle",
            "description": "Toggle a Home Assistant entity on/off.",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ha_call_service",
            "description": "Call any Home Assistant service (e.g. set brightness, temperature). domain+service+entity_id required.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "e.g. light, climate, switch"},
                    "service": {"type": "string", "description": "e.g. turn_on, set_temperature"},
                    "entity_id": {"type": "string"},
                    "brightness": {"type": "integer", "description": "0-255 for lights"},
                    "temperature": {"type": "number", "description": "For climate entities"},
                },
                "required": ["domain", "service", "entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ha_get_state",
            "description": "Get the current state of a single Home Assistant entity by exact entity_id.",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ha_get_states",
            "description": "Get states of all entities in given domains (e.g. light, switch). Shows which are on/off.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domains": {"type": "array", "items": {"type": "string"}, "description": "e.g. [\"light\"]"},
                },
                "required": [],
            },
        },
    },
    # ── AnyList ──
    {
        "type": "function",
        "function": {
            "name": "anylist_get_list",
            "description": "Get items on an AnyList shopping list. If list_name is given, returns unchecked items in that list (pass include_checked=true for all). If list_name is omitted, returns the names of all available lists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "list_name": {"type": "string", "description": "Name of the shopping list (e.g. 'Groceries'). Omit to list all available lists."},
                    "include_checked": {"type": "boolean", "description": "Include already-checked/crossed-off items. Default false."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "anylist_get_meal_plan",
            "description": "Get the meal plan from AnyList for a date range. Use when the user asks what's for dinner, what meals are planned, etc. start and end are ISO 8601 dates (YYYY-MM-DD).",
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "Start date ISO 8601 e.g. 2026-05-08"},
                    "end": {"type": "string", "description": "End date ISO 8601, inclusive. Defaults to start (single day)."},
                },
                "required": ["start"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_calendar_events",
            "description": (
                "Get calendar events from CalDAV (Nextcloud) for a date range. "
                "start and end are ISO 8601 dates or datetimes e.g. 2026-05-08 or 2026-05-08T00:00:00."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "Start of range (ISO 8601)"},
                    "end": {"type": "string", "description": "End of range (ISO 8601)"},
                },
                "required": ["start", "end"],
            },
        },
    },
]

_HA_TOOL_NAMES = frozenset({"ha_turn_on", "ha_turn_off", "ha_toggle", "ha_call_service", "ha_get_state", "ha_get_states"})
_WEATHER_TOOL_NAMES = frozenset({"ha_get_weather"})
_CALDAV_TOOL_NAMES = frozenset({"get_calendar_events"})
_ANYLIST_TOOL_NAMES = frozenset({"anylist_get_list", "anylist_get_meal_plan"})


def get_active_tool_definitions() -> list[dict]:
    """Return only tool definitions for services that are configured."""
    ha_enabled = bool(Config.HA_URL and Config.HA_TOKEN)
    weather_enabled = ha_enabled and bool(Config.HA_WEATHER_ENTITY)
    caldav_enabled = bool(Config.CALDAV_URL and Config.CALDAV_USERNAME and Config.CALDAV_PASSWORD)
    anylist_enabled = bool(Config.ANYLIST_EMAIL and Config.ANYLIST_PASSWORD)

    active = []
    for td in TOOL_DEFINITIONS:
        name = td["function"]["name"]
        if name in _HA_TOOL_NAMES and not ha_enabled:
            continue
        if name in _WEATHER_TOOL_NAMES and not weather_enabled:
            continue
        if name in _CALDAV_TOOL_NAMES and not caldav_enabled:
            continue
        if name in _ANYLIST_TOOL_NAMES and not anylist_enabled:
            continue
        active.append(td)
    return active


def _normalize_calendar_args(args: dict) -> dict:
    """Coerce common parameter-name variants to canonical names before dispatching."""
    from datetime import timedelta as _td

    # --- Step 1: Combine split date + time fields ---
    import re as _re
    _FULL_DT = _re.compile(r"^\d{4}-\d{2}-\d{2}")
    _TIME_ONLY = _re.compile(r"^\d{2}:\d{2}")

    s_date = args.pop("start_date", None)
    s_time = args.pop("start_time", None)
    e_date = args.pop("end_date", None)
    e_time = args.pop("end_time", None)

    # If *_time fields actually hold full datetimes, treat them as such
    if s_time and _FULL_DT.match(str(s_time)):
        if not s_date:
            s_date = str(s_time)[:10]
        if "new_start" not in args:
            args["new_start"] = s_time
        s_time = None
    if e_time and _FULL_DT.match(str(e_time)):
        if not e_date:
            e_date = str(e_time)[:10]
        if "new_end" not in args:
            args["new_end"] = e_time
        e_time = None

    if s_date and s_time:
        if "start" not in args:
            args["start"] = s_date
        if "new_start" not in args:
            args["new_start"] = f"{s_date}T{s_time}"
    elif s_date and "start" not in args:
        args["start"] = s_date
    elif s_time and _TIME_ONLY.match(str(s_time)):
        base = (args.get("start") or "")[:10]
        if len(base) == 10 and "new_start" not in args:
            args["new_start"] = f"{base}T{s_time}"

    if e_date and e_time:
        if "new_end" not in args:
            args["new_end"] = f"{e_date}T{e_time}"
        if "end" not in args:
            args["end"] = e_date
    elif e_date and "end" not in args:
        args["end"] = e_date
    elif e_time and _TIME_ONLY.match(str(e_time)):
        base = (args.get("new_start") or args.get("start") or "")[:10]
        if len(base) == 10 and "new_end" not in args:
            args["new_end"] = f"{base}T{e_time}"

    # --- Step 2: Simple renames ---
    renames = {
        "startdatetime": "start",
        "start_datetime": "start",
        "date": "start",
        "event_date": "start",
        "enddatetime": "end",
        "end_datetime": "end",
        "title": "summary",
        "event_title": "summary",
        "event_name": "summary",
        "name": "summary",
        "calendar_id": "calendar_name",
        "cal_name": "calendar_name",
        "calendar": "calendar_name",
    }
    for old, new in renames.items():
        if old in args and new not in args:
            args[new] = args.pop(old)

    # --- Step 3: Resolve relative date words ---
    _RELATIVE = {
        "today":     lambda now: now.strftime("%Y-%m-%d"),
        "tomorrow":  lambda now: (now + _td(days=1)).strftime("%Y-%m-%d"),
        "yesterday": lambda now: (now - _td(days=1)).strftime("%Y-%m-%d"),
    }
    for field in ("start", "end", "new_start", "new_end"):
        val = args.get(field)
        if isinstance(val, str) and val.lower() in _RELATIVE:
            args[field] = _RELATIVE[val.lower()](_now_local())
            logger.info(f"Resolved relative date '{val}' -> '{args[field]}'")

    if "calendar_name" in args and args["calendar_name"]:
        args["calendar_name"] = args["calendar_name"].strip()

    # Fix hallucinated years in any date field
    for field in ("start", "end", "new_start", "new_end"):
        if isinstance(args.get(field), str):
            args[field] = _fix_year(args[field])

    return args


_TOOL_ALIASES: dict[str, str] = {
    # calendar read aliases
    "calendar_get": "get_calendar_events",
    "get_events": "get_calendar_events",
    "list_events": "get_calendar_events",
    "getcalendarevents": "get_calendar_events",
    "listcalendarevents": "get_calendar_events",
    # anylist aliases
    "get_shopping_list": "anylist_get_list",
    "get_list": "anylist_get_list",
    "shopping_list": "anylist_get_list",
    "get_meal_plan": "anylist_get_meal_plan",
    "get_meals": "anylist_get_meal_plan",
    "get_dinner": "anylist_get_meal_plan",
    "whats_for_dinner": "anylist_get_meal_plan",
    # HA control aliases
    "ha_turnoff": "ha_turn_off",
    "haturnoff": "ha_turn_off",
    "haturnofflight": "ha_turn_off",
    "turnofflight": "ha_turn_off",
    "ha_turnon": "ha_turn_on",
    "haturnonlight": "ha_turn_on",
    "turnonlight": "ha_turn_on",
    "hatogglelight": "ha_toggle",
    "ha_toggle_light": "ha_toggle",
    # HA weather aliases — model invents many names for this
    "ha_weather_api": "ha_get_weather",
    "ha_weather": "ha_get_weather",
    "weather_api": "ha_get_weather",
    "get_weather": "ha_get_weather",
    "weather": "ha_get_weather",
    "current_weather": "ha_get_weather",
    "fetch_weather": "ha_get_weather",
    # todo aliases
    "add_task": "todo_add_item",
    "add_todo": "todo_add_item",
    "add_item": "todo_add_item",
    "add_to_list": "todo_add_item",
    "create_list": "todo_create_list",
    "make_list": "todo_create_list",
    "new_list": "todo_create_list",
    "delete_list": "todo_delete_list",
    "remove_list": "todo_delete_list",
    "get_list_items": "todo_get_items",
    "list_items": "todo_get_items",
    # reminder aliases
    "create_reminder": "reminder_create",
    "add_reminder": "reminder_create",
    "set_reminder": "reminder_create",
    "snooze_reminder": "reminder_snooze",
    "snooze": "reminder_snooze",
    # web search aliases
    "search": "search_web",
    "google_search": "search_web",
    "web_search": "search_web",
    "internet_search": "search_web",
    "tavily_search": "search_web",
    # memory aliases
    "memory": "memory_save",
    "store_memory": "memory_save",
    "save_memory": "memory_save",
    "add_memory": "memory_save",
    "set_memory": "memory_save",
    "forget_memory": "memory_delete",
}


def _normalize_reminder_args(args: dict) -> dict:
    """Coerce various model-invented parameter shapes into a valid fire_at ISO string."""
    from datetime import timedelta
    if "fire_at" in args:
        args["fire_at"] = _fix_year(args["fire_at"])
        return args

    now = _now_local()

    # Relative minute/hour offsets the model sometimes invents
    minutes = 0
    for key in ("time_minutes", "minutes", "delay_minutes", "offset_minutes", "in_minutes"):
        if key in args:
            try:
                minutes += int(args.pop(key))
            except (ValueError, TypeError):
                pass
    for key in ("time_hours", "hours", "delay_hours", "in_hours"):
        if key in args:
            try:
                minutes += int(args.pop(key)) * 60
            except (ValueError, TypeError):
                pass
    if minutes:
        args["fire_at"] = (now + timedelta(minutes=minutes)).isoformat()
        return args

    # Model passed a "time" or "datetime" field with an ISO string or partial string
    for key in ("time", "datetime", "at", "scheduled_time", "fire_time", "reminder_time"):
        val = args.pop(key, None)
        if val and isinstance(val, str):
            val = _fix_year(val)
            # If it's a time-only string like "14:30", combine with today's date
            import re as _re
            if _re.match(r"^\d{2}:\d{2}(:\d{2})?$", val.strip()):
                val = now.strftime("%Y-%m-%d") + "T" + val.strip()
            args["fire_at"] = val
            return args

    return args


_HA_ENTITY_PARAM_ALIASES = ("lightid", "light_id", "entity", "device", "device_id", "id", "light")
_HA_TOOLS = frozenset({"ha_turn_on", "ha_turn_off", "ha_toggle", "ha_call_service", "ha_get_state", "ha_get_states"})
_HA_ENTITY_ID_RE = re.compile(r"^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$")
_HA_SERVICE_RE = re.compile(r"^[a-zA-Z0-9_]+$")


async def _normalize_ha_args(name: str, args: dict, user_id: int) -> dict:
    """Normalise HA parameter names and resolve placeholder entity_ids from memory."""
    # Coerce alternative param names → entity_id
    if "entity_id" not in args:
        for alt in _HA_ENTITY_PARAM_ALIASES:
            if alt in args:
                args["entity_id"] = args.pop(alt)
                break

    entity_id = args.get("entity_id", "")
    # A real HA entity_id always contains a dot (e.g. light.office).
    # If the model passed a descriptive placeholder, look it up from memory.
    if entity_id and "." not in entity_id:
        memories = await db.get_memories(user_id)
        entity_mems = [(m["key"], m["value"]) for m in memories if "." in m["value"]]
        placeholder = entity_id.lower()
        best, best_score = None, 0
        for key, value in entity_mems:
            key_words = set(re.sub(r"entity[_\s]?id", "", key, flags=re.IGNORECASE).split()) - {"the", "for", "of"}
            score = sum(1 for w in key_words if w and w in placeholder)
            if score > best_score:
                best_score, best = score, value
        if best:
            logger.info(f"Resolved placeholder entity_id {entity_id!r} → {best!r} from memory")
            args["entity_id"] = best

    return args


async def handle_tool_call(name: str, args: dict, user_id: int) -> str:
    # Strip namespace prefix — supports both dot and colon separators
    # e.g. "reminder_api.create_reminder" -> "create_reminder"
    # e.g. "google:search" -> "search"
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    if ":" in name:
        name = name.rsplit(":", 1)[-1]

    # Resolve method-dispatch pattern: model passes method='delete_event' as an arg
    if "method" in args:
        method_val = str(args.pop("method")).lower().replace(" ", "_")
        resolved = _TOOL_ALIASES.get(method_val, method_val)
        logger.info(f"Method-dispatch: {name}(method={method_val!r}) -> {resolved}")
        name = resolved

    # Meta-dispatcher: model called "any_tool_name" or similar with the real tool name in args
    for _meta_key in ("any_tool_name", "tool_name", "function_name", "function", "dispatch"):
        if _meta_key in args:
            _actual = str(args.pop(_meta_key)).lower().replace(" ", "_")
            _actual = _TOOL_ALIASES.get(_actual, _actual)
            logger.info(f"Meta-dispatch: {name}({_meta_key}={_actual!r}) -> {_actual}")
            name = _actual
            break

    # Resolve alias (model hallucinated tool name)
    if name in _TOOL_ALIASES:
        name = _TOOL_ALIASES[name]

    # Normalize todo_delete_list / todo_create_list arg name variants
    if name in ("todo_delete_list", "todo_create_list"):
        if "list_name" in args and "name" not in args:
            args["name"] = args.pop("list_name")

    # Normalize todo_add_item arg name variants
    if name == "todo_add_item":
        if "task" in args and "content" not in args:
            args["content"] = args.pop("task")
        if "item" in args and "content" not in args:
            args["content"] = args.pop("item")
        if "list" in args and "list_name" not in args:
            args["list_name"] = args.pop("list")
        if "name" in args and "list_name" not in args:
            args["list_name"] = args.pop("name")

    # Normalize calendar parameter names regardless of which alias was used
    if any(x in name for x in ("calendar", "event")):
        args = _normalize_calendar_args(args)

    # Normalize reminder args (flexible fire_at, year fix)
    if name == "reminder_create":
        args = _normalize_reminder_args(args)

    # Normalize HA args: fix param aliases and resolve placeholder entity_ids
    if name in _HA_TOOLS:
        args = await _normalize_ha_args(name, args, user_id)

    try:
        match name:
            # ── Todo ──
            case "todo_create_list":
                result = await db.create_todo_list(user_id, args["name"])
                return f"Created **{result['name'].title()}**."

            case "todo_delete_list":
                name = _normalize_list_name(args["name"])
                ok = await db.delete_todo_list(user_id, name)
                return f"Deleted list **{name.title()}**." if ok else f"No list named **{name.title()}** found."

            case "todo_get_lists":
                lists = await db.get_todo_lists(user_id)
                if not lists:
                    return "No to-do lists found."
                return json.dumps(lists)

            case "todo_add_item":
                list_name = _normalize_list_name(args["list_name"])
                result = await db.add_todo_item(user_id, list_name, args["content"])
                return f"Added {args['content']} to **{list_name.title()}**."

            case "todo_get_items":
                list_name = _normalize_list_name(args["list_name"])
                items = await db.get_todo_items(user_id, list_name)
                if not items:
                    return f"**{list_name.title()}** is empty."
                lines = [f"**{list_name.title()}:**"]
                for i, item in enumerate(items, 1):
                    lines.append(f"{i}. {item['content']}")
                return "\n".join(lines)

            case "todo_delete_item":
                ok = await db.delete_todo_item(args["item_id"], user_id)
                return "Item deleted." if ok else f"Item id={args['item_id']} not found."

            case "todo_clear_list":
                list_name = _normalize_list_name(args["list_name"])
                count = await db.clear_todo_list(user_id, list_name)
                return f"Cleared **{list_name.title()}** ({count} item{'s' if count != 1 else ''} removed)."

            # ── Memory ──
            case "memory_save":
                await db.save_memory(user_id, args["key"], args["value"])
                return f"Remembered: {args['key']} = {args['value']}"

            case "memory_delete":
                ok = await db.delete_memory(user_id, args["key"])
                return "Forgotten." if ok else f"No memory found for {args['key']}."

            case "memory_list":
                memories = await db.get_memories(user_id)
                if not memories:
                    return "No saved facts yet."
                return "\n".join(f"- {m['key']}: {m['value']}" for m in memories)

            # ── Reminders ──
            case "reminder_create":
                if "fire_at" not in args:
                    return "Error: reminder_create requires fire_at as an ISO 8601 datetime (e.g. 2026-05-08T15:30:00)."
                fire_at_str = args["fire_at"]
                _parse_datetime(fire_at_str)
                smart = bool(args.get("smart", False))
                message = _normalize_reminder_message(args["message"])
                reminder = await db.create_reminder(
                    user_id=user_id,
                    message=message,
                    fire_at=fire_at_str,
                    recurrence=args.get("recurrence"),
                    recurrence_human=args.get("recurrence_human"),
                    smart=smart,
                )
                await sched.schedule_reminder(reminder)
                if reminder.get("recurrence_human"):
                    human = reminder["recurrence_human"]
                else:
                    fire_dt = _parse_datetime(fire_at_str)
                    now = _now_local()
                    delta_mins = int((fire_dt - now).total_seconds() / 60)
                    if 0 < delta_mins < 60:
                        human = f"in {delta_mins} minute{'s' if delta_mins != 1 else ''}"
                    elif 60 <= delta_mins < 120:
                        human = "in 1 hour"
                    elif 120 <= delta_mins < 1440:
                        human = f"in {delta_mins // 60} hours"
                    else:
                        time_str = fire_dt.strftime("%I:%M %p").lstrip("0")
                        date_str = ""
                        if fire_dt.date() != now.date():
                            date_str = " on " + fire_dt.strftime("%A, %B %-d")
                        human = f"at {time_str}{date_str}"
                kind = "Smart reminder" if smart else "Reminder"
                return f"{kind} set: **{message}** {human}."

            case "reminder_list":
                reminders = await db.get_reminders(user_id)
                if not reminders:
                    return "No active reminders."
                lines = []
                for r in reminders:
                    try:
                        fire_dt = _parse_datetime(r["fire_at"])
                        time_str = fire_dt.strftime("%I:%M %p").lstrip("0")
                        date_str = fire_dt.strftime("%a, %b %-d")
                        when = f"{date_str} at {time_str}"
                    except Exception:
                        when = r["fire_at"]
                    recur = f" ({r['recurrence_human']})" if r.get("recurrence_human") else ""
                    kind = " [smart]" if r.get("smart") else ""
                    lines.append(f"• {r['message']}{kind} — {when}{recur} [#{r['id']}]")
                return "\n".join(lines)

            case "reminder_delete":
                rid = args["reminder_id"]
                ok = await db.delete_reminder(user_id, rid)
                if ok:
                    await sched.unschedule_reminder(rid)
                    return f"Reminder {rid} deleted."
                return f"Reminder {rid} not found."

            case "reminder_snooze":
                from datetime import timedelta
                duration_str = args.get("duration", "").strip()
                reminder_id = args.get("reminder_id")

                # No duration — ask the user
                if not duration_str:
                    return "How long would you like to snooze? (e.g. 10 minutes, 1 hour)"

                minutes = _parse_snooze_duration(duration_str)
                if not minutes:
                    return f"I couldn't understand '{duration_str}'. Try something like '10 minutes' or '1 hour'."

                # Resolve which reminder to snooze
                if not reminder_id:
                    reminder_id = sched.get_last_fired_reminder_id(user_id)
                if not reminder_id:
                    return "I'm not sure which reminder to snooze. Please say 'snooze reminder <id> for <duration>'."

                reminder = await db.get_reminder_by_id(reminder_id, user_id)
                if not reminder:
                    return f"Reminder {reminder_id} not found."

                new_fire_at = _now_local() + timedelta(minutes=minutes)
                new_fire_at_str = new_fire_at.isoformat()

                if reminder.get("recurrence"):
                    # Recurring: schedule a one-shot extra fire without disturbing the recurrence
                    scheduler = sched.get_scheduler()
                    if scheduler:
                        from apscheduler.triggers.date import DateTrigger
                        snooze_job_id = f"reminder_{reminder_id}_snooze"
                        scheduler.add_job(
                            sched._fire_reminder,
                            trigger=DateTrigger(run_date=new_fire_at),
                            id=snooze_job_id,
                            kwargs={
                                "reminder_id": reminder_id,
                                "user_id": user_id,
                                "message": reminder["message"],
                                "original_time": new_fire_at_str,
                                "late": False,
                            },
                            replace_existing=True,
                        )
                else:
                    await db.snooze_reminder(reminder_id, new_fire_at_str)
                    await sched.schedule_reminder({**reminder, "fire_at": new_fire_at_str, "fired": 0})

                human_time = new_fire_at.strftime("%I:%M %p").lstrip("0")
                return f"Snoozed. I'll remind you about **{reminder['message']}** at {human_time}."

            # ── Web Search ──
            case "search_web":
                query = args.get("query", "")
                if not query:
                    return "Error: search_web requires a 'query' argument."
                data = await search_service.search(query)
                results = data["results"]
                if not results:
                    return "No search results found."
                return json.dumps(results[:5])

            # ── Home Assistant ──
            case "ha_turn_on":
                if not (Config.HA_URL and Config.HA_TOKEN):
                    return "Home Assistant is not configured."
                entity_id = args["entity_id"]
                if not _HA_ENTITY_ID_RE.fullmatch(entity_id):
                    return "Error: invalid Home Assistant entity_id."
                domain = entity_id.split(".")[0]
                await ha.call_service(domain, "turn_on", entity_id)
                return f"Turned on {entity_id}."

            case "ha_turn_off":
                if not (Config.HA_URL and Config.HA_TOKEN):
                    return "Home Assistant is not configured."
                entity_id = args["entity_id"]
                if not _HA_ENTITY_ID_RE.fullmatch(entity_id):
                    return "Error: invalid Home Assistant entity_id."
                domain = entity_id.split(".")[0]
                await ha.call_service(domain, "turn_off", entity_id)
                return f"Turned off {entity_id}."

            case "ha_toggle":
                if not (Config.HA_URL and Config.HA_TOKEN):
                    return "Home Assistant is not configured."
                entity_id = args["entity_id"]
                if not _HA_ENTITY_ID_RE.fullmatch(entity_id):
                    return "Error: invalid Home Assistant entity_id."
                domain = entity_id.split(".")[0]
                await ha.call_service(domain, "toggle", entity_id)
                return f"Toggled {entity_id}."

            case "ha_call_service":
                if not (Config.HA_URL and Config.HA_TOKEN):
                    return "Home Assistant is not configured."
                entity_id = args.get("entity_id", "")
                domain = args.get("domain") or (entity_id.split(".")[0] if "." in entity_id else "")
                service = args.get("service", "")
                if not domain or not service:
                    return "Error: ha_call_service requires domain, service, and entity_id."
                if not _HA_ENTITY_ID_RE.fullmatch(entity_id):
                    return "Error: invalid Home Assistant entity_id."
                if not _HA_SERVICE_RE.fullmatch(domain) or not _HA_SERVICE_RE.fullmatch(service):
                    return "Error: invalid Home Assistant domain or service."
                entity_domain = entity_id.split(".")[0]
                if domain != entity_domain:
                    return "Error: Home Assistant service domain must match the entity domain."
                extra = {k: v for k, v in args.items() if k not in ("domain", "service", "entity_id")}
                await ha.call_service(domain, service, entity_id, extra or None)
                return f"Called {domain}.{service} on {entity_id}."

            case "ha_get_state":
                if not (Config.HA_URL and Config.HA_TOKEN):
                    return "Home Assistant is not configured."
                if not _HA_ENTITY_ID_RE.fullmatch(args["entity_id"]):
                    return "Error: invalid Home Assistant entity_id."
                state = await ha.get_entity_state(args["entity_id"])
                return f"{state['friendly_name']} ({state['entity_id']}): {state['state']}"

            case "ha_get_states":
                if not (Config.HA_URL and Config.HA_TOKEN):
                    return "Home Assistant is not configured."
                states = await ha.get_states(domains=args.get("domains"))
                if not states:
                    return "No entities found."
                on_states = [s for s in states if s["state"] == "on"]
                off_states = [s for s in states if s["state"] == "off"]
                lines = []
                if on_states:
                    lines.append("ON: " + ", ".join(f"{s['friendly_name']} ({s['entity_id']})" for s in on_states))
                if off_states:
                    lines.append("OFF: " + ", ".join(f"{s['friendly_name']} ({s['entity_id']})" for s in off_states[:10]))
                return "\n".join(lines) or "No entities found."

            # ── AnyList ──
            case "anylist_get_list":
                if not Config.ANYLIST_EMAIL:
                    return "AnyList is not configured. Set ANYLIST_EMAIL and ANYLIST_PASSWORD."
                list_name = args.get("list_name")
                include_checked = bool(args.get("include_checked", False))
                if list_name:
                    items = await anylist_service.get_list_items(list_name, include_checked)
                    if not items:
                        label = "items" if include_checked else "unchecked items"
                        return f"No {label} in **{list_name.title()}**."
                    lines = [f"**On your {list_name.title()} list:**"]
                    for item in items:
                        label = item["name"]
                        if item["quantity"]:
                            label = f"{item['quantity']} {label}"
                        if item["details"]:
                            label += f" ({item['details']})"
                        if item["category"]:
                            label += f" [{item['category']}]"
                        lines.append(f"• {label}")
                    return "\n".join(lines)
                else:
                    lists = await anylist_service.get_lists()
                    if not lists:
                        return "No AnyList shopping lists found."
                    return "Available lists: " + ", ".join(l["name"] for l in lists)

            case "anylist_get_meal_plan":
                if not Config.ANYLIST_EMAIL:
                    return "AnyList is not configured. Set ANYLIST_EMAIL and ANYLIST_PASSWORD."
                start = args.get("start") or args.get("date") or _now_local().strftime("%Y-%m-%d")
                end = args.get("end") or args.get("end_date") or start
                meals = await anylist_service.get_meal_plan(start, end)
                if not meals:
                    date_range = start if start == end else f"{start} to {end}"
                    return f"No meals planned for {date_range}."
                from datetime import date as _date
                lines = []
                for meal in meals:
                    line = meal["meal"]
                    if start != end:
                        try:
                            d = _date.fromisoformat(meal["date"])
                            label = d.strftime("%A, %b %-d")
                        except ValueError:
                            label = meal["date"]
                        line = f"{label}: {line}"
                    if meal.get("notes"):
                        line += f" — {meal['notes']}"
                    lines.append(line)
                return "\n".join(lines)

            case "get_calendar_events":
                if not (Config.CALDAV_URL and Config.CALDAV_USERNAME and Config.CALDAV_PASSWORD):
                    return "CalDAV is not configured. Set CALDAV_URL, CALDAV_USERNAME, and CALDAV_PASSWORD."
                if "start" not in args:
                    args["start"] = _now_local().strftime("%Y-%m-%d")
                if "end" not in args:
                    args["end"] = args["start"]
                events = await cal_service.get_calendar_events(args["start"], args["end"])
                if not events:
                    return "No events found in that date range."
                from collections import defaultdict as _defaultdict
                from datetime import date as _date
                by_date: dict = _defaultdict(list)
                errors = []
                for e in events:
                    if "error" in e:
                        errors.append(f"⚠️ {e['calendar']}: {e['error']}")
                        continue
                    by_date[e["start"][:10]].append(e)
                lines = list(errors)
                for date_str in sorted(by_date):
                    try:
                        day_label = _date.fromisoformat(date_str).strftime("%A, %B %-d")
                    except ValueError:
                        day_label = date_str
                    lines.append(f"\n**{day_label}**")
                    for e in by_date[date_str]:
                        start = e["start"]
                        if e["all_day"]:
                            time_str = "All day"
                        elif "T" in start:
                            dt = datetime.fromisoformat(start)
                            time_str = dt.strftime("%I:%M %p").lstrip("0")
                        else:
                            time_str = start
                        loc = f" ({e['location']})" if e.get("location") else ""
                        desc = f"\n    {e['description']}" if e.get("description") else ""
                        lines.append(f"  • {time_str} — {e['summary']}{loc}{desc}")
                return "\n".join(lines).strip()

            case "ha_get_weather":
                if not (Config.HA_URL and Config.HA_TOKEN):
                    return "Home Assistant is not configured."
                if not Config.HA_WEATHER_ENTITY:
                    return "Weather entity not configured. Set HA_WEATHER_ENTITY in your environment."
                weather = await ha.get_weather()
                temp = f"{weather['temperature']}{weather['temperature_unit']}" if weather.get("temperature") is not None else "unknown"
                wind = f"{weather['wind_speed']}{weather['wind_speed_unit']}" if weather.get("wind_speed") is not None else "unknown"
                humidity = f"{weather['humidity']}%" if weather.get("humidity") is not None else "unknown"
                lines = [
                    f"Condition: {weather['condition']}",
                    f"Temperature: {temp}",
                    f"Humidity: {humidity}",
                    f"Wind: {wind}",
                ]
                forecast = weather.get("forecast", [])
                if forecast:
                    lines.append("Forecast:")
                    for entry in forecast:
                        dt = entry.get("datetime", "")
                        date_str = dt[:10] if dt else "?"
                        high = entry.get("temperature", "?")
                        low = entry.get("templow", "?")
                        condition = entry.get("condition", "?")
                        lines.append(f"  {date_str}: {condition}, high {high}, low {low}")
                return "\n".join(lines)

            case _:
                return f"Unknown tool: {name}"

    except PermissionError as e:
        return f"Permission denied: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.error(f"Tool '{name}' failed: {e}", exc_info=True)
        return f"Tool error: {e}"
