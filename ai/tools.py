"""
Tool definitions and handlers for the Ollama AI agent.
"""

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import database as db
import scheduler as sched
from config import Config
from services import calendar as cal_service
from services import homeassistant as ha
from services import search as search_service

logger = logging.getLogger(__name__)


def _normalize_list_name(name: str) -> str:
    """Strip trailing 'list'/'lists' that models often append."""
    import re
    return re.sub(r"\s+lists?$", "", name, flags=re.IGNORECASE).strip()


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
            "name": "todo_update_item",
            "description": "Update a to-do item's text or mark it done/undone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "content": {"type": "string"},
                    "done": {"type": "boolean"},
                },
                "required": ["item_id"],
            },
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
    # ── Notes ──
    {
        "type": "function",
        "function": {
            "name": "note_create",
            "description": "Create a note with title, content, and optional tags.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "tags": {"type": "string", "description": "Comma-separated tags"},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "note_search",
            "description": "Search notes by keyword.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "note_update",
            "description": "Update an existing note by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "tags": {"type": "string"},
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "note_delete",
            "description": "Delete a note by ID.",
            "parameters": {"type": "object", "properties": {"note_id": {"type": "integer"}}, "required": ["note_id"]},
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

    return args


_TOOL_ALIASES: dict[str, str] = {
    # calendar read aliases
    "calendar_get": "get_calendar_events",
    "get_events": "get_calendar_events",
    "list_events": "get_calendar_events",
    "getcalendarevents": "get_calendar_events",
    "listcalendarevents": "get_calendar_events",
    # memory aliases
    "store_memory": "memory_save",
    "save_memory": "memory_save",
    "add_memory": "memory_save",
    "set_memory": "memory_save",
    "forget_memory": "memory_delete",
}


async def handle_tool_call(name: str, args: dict, user_id: int) -> str:
    # Resolve method-dispatch pattern: model passes method='delete_event' as an arg
    if "method" in args:
        method_val = str(args.pop("method")).lower().replace(" ", "_")
        resolved = _TOOL_ALIASES.get(method_val, method_val)
        logger.info(f"Method-dispatch: {name}(method={method_val!r}) -> {resolved}")
        name = resolved

    # Resolve alias (model hallucinated tool name)
    if name in _TOOL_ALIASES:
        name = _TOOL_ALIASES[name]

    # Normalize calendar parameter names regardless of which alias was used
    if any(x in name for x in ("calendar", "event")):
        args = _normalize_calendar_args(args)

    try:
        match name:
            # ── Todo ──
            case "todo_create_list":
                result = await db.create_todo_list(user_id, args["name"])
                return f"Created list '{result['name']}' (id={result['id']})."

            case "todo_delete_list":
                name = _normalize_list_name(args["name"])
                ok = await db.delete_todo_list(user_id, name)
                return f"Deleted list '{name}'." if ok else f"No list named '{name}' found."

            case "todo_get_lists":
                lists = await db.get_todo_lists(user_id)
                if not lists:
                    return "No to-do lists found."
                return json.dumps(lists)

            case "todo_add_item":
                list_name = _normalize_list_name(args["list_name"])
                result = await db.add_todo_item(user_id, list_name, args["content"])
                return f"Added item (id={result['id']}) to '{list_name}'."

            case "todo_get_items":
                list_name = _normalize_list_name(args["list_name"])
                items = await db.get_todo_items(user_id, list_name)
                if not items:
                    return f"List '{args['list_name']}' is empty."
                lines = []
                for i, item in enumerate(items, 1):
                    status = "✅ Done" if item["done"] else "☐ Pending"
                    lines.append(f"{i}. {item['content']} ({status})")
                return "\n".join(lines)

            case "todo_update_item":
                ok = await db.update_todo_item(
                    args["item_id"], content=args.get("content"), done=args.get("done")
                )
                return "Item updated." if ok else f"Item id={args['item_id']} not found."

            case "todo_delete_item":
                ok = await db.delete_todo_item(args["item_id"])
                return "Item deleted." if ok else f"Item id={args['item_id']} not found."

            # ── Memory ──
            case "memory_save":
                await db.save_memory(user_id, args["key"], args["value"])
                return f"Remembered: {args['key']} = {args['value']}"

            case "memory_delete":
                ok = await db.delete_memory(user_id, args["key"])
                return f"Forgotten." if ok else f"No memory found for '{args['key']}'."

            case "memory_list":
                memories = await db.get_memories(user_id)
                if not memories:
                    return "No saved facts yet."
                return "\n".join(f"- {m['key']}: {m['value']}" for m in memories)

            # ── Reminders ──
            case "reminder_create":
                fire_at_str = args["fire_at"]
                _parse_datetime(fire_at_str)
                smart = bool(args.get("smart", False))
                reminder = await db.create_reminder(
                    user_id=user_id,
                    message=args["message"],
                    fire_at=fire_at_str,
                    recurrence=args.get("recurrence"),
                    recurrence_human=args.get("recurrence_human"),
                    smart=smart,
                )
                await sched.schedule_reminder(reminder)
                human = reminder.get("recurrence_human") or f"at {fire_at_str}"
                kind = "Smart reminder" if smart else "Reminder"
                return f"{kind} created (id={reminder['id']}): '{args['message']}' {human}."

            case "reminder_list":
                reminders = await db.get_reminders(user_id)
                if not reminders:
                    return "No active reminders."
                return json.dumps(reminders)

            case "reminder_delete":
                rid = args["reminder_id"]
                ok = await db.delete_reminder(user_id, rid)
                if ok:
                    await sched.unschedule_reminder(rid)
                    return f"Reminder {rid} deleted."
                return f"Reminder {rid} not found."

            # ── Notes ──
            case "note_create":
                note = await db.create_note(user_id, args["title"], args["content"], args.get("tags", ""))
                return f"Note created (id={note['id']}): '{note['title']}'."

            case "note_search":
                notes = await db.search_notes(user_id, args["query"])
                if not notes:
                    return "No notes found matching that query."
                summaries = [{"id": n["id"], "title": n["title"], "tags": n["tags"], "updated_at": n["updated_at"]} for n in notes]
                return json.dumps(summaries)

            case "note_update":
                ok = await db.update_note(
                    args["note_id"], user_id,
                    title=args.get("title"), content=args.get("content"), tags=args.get("tags"),
                )
                return "Note updated." if ok else f"Note id={args['note_id']} not found."

            case "note_delete":
                ok = await db.delete_note(args["note_id"], user_id)
                return "Note deleted." if ok else f"Note id={args['note_id']} not found."

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
                domain = entity_id.split(".")[0]
                await ha.call_service(domain, "turn_on", entity_id)
                return f"Turned on {entity_id}."

            case "ha_turn_off":
                if not (Config.HA_URL and Config.HA_TOKEN):
                    return "Home Assistant is not configured."
                entity_id = args["entity_id"]
                domain = entity_id.split(".")[0]
                await ha.call_service(domain, "turn_off", entity_id)
                return f"Turned off {entity_id}."

            case "ha_toggle":
                if not (Config.HA_URL and Config.HA_TOKEN):
                    return "Home Assistant is not configured."
                entity_id = args["entity_id"]
                await ha.call_service("homeassistant", "toggle", entity_id)
                return f"Toggled {entity_id}."

            case "ha_call_service":
                if not (Config.HA_URL and Config.HA_TOKEN):
                    return "Home Assistant is not configured."
                entity_id = args.get("entity_id", "")
                domain = args.get("domain") or (entity_id.split(".")[0] if "." in entity_id else "")
                service = args.get("service", "")
                if not domain or not service:
                    return "Error: ha_call_service requires domain, service, and entity_id."
                extra = {k: v for k, v in args.items() if k not in ("domain", "service", "entity_id")}
                await ha.call_service(domain, service, entity_id, extra or None)
                return f"Called {domain}.{service} on {entity_id}."

            case "ha_get_state":
                if not (Config.HA_URL and Config.HA_TOKEN):
                    return "Home Assistant is not configured."
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
                lines = []
                for e in events:
                    if "error" in e:
                        lines.append(f"⚠️ {e['calendar']}: {e['error']}")
                        continue
                    start = e["start"]
                    if e["all_day"]:
                        time_str = "All day"
                    elif "T" in start:
                        dt = datetime.fromisoformat(start)
                        time_str = dt.strftime("%I:%M %p").lstrip("0")
                    else:
                        time_str = start
                    loc = f" ({e['location']})" if e.get("location") else ""
                    desc = f"\n  {e['description']}" if e.get("description") else ""
                    lines.append(f"• {time_str} — {e['summary']}{loc}{desc}")
                return "\n".join(lines)

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
