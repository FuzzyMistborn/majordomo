"""
AI agent: manages per-user conversation history and drives the Ollama tool-calling loop.
"""

import json
import os
import logging
import re
import ast
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import ollama

import database as db
import scheduler as sched
from ai.tools import TOOL_DEFINITIONS, _TOOL_ALIASES, get_active_tool_definitions, handle_tool_call
from config import Config

logger = logging.getLogger(__name__)

# Built once: all tool names the parser should recognise in bare funcname(...) text
_KNOWN_TOOLS: frozenset[str] = frozenset(
    {td["function"]["name"] for td in TOOL_DEFINITIONS} | set(_TOOL_ALIASES.keys())
)

# Personalities are loaded lazily and cached by slug.
_personality_cache: dict[str, str] = {}
_PERSONALITY_SETTING_KEY = "personality"
_DEFAULT_PERSONALITY = "wit"


def _personality_dirs() -> list[str]:
    return ["/app/personalities", "personalities"]


def _personality_paths(name: str) -> list[str]:
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "", name.lower())
    paths = [os.path.join(path, f"{safe_name}.md") for path in _personality_dirs()]
    if safe_name in (_DEFAULT_PERSONALITY, "default"):
        paths.extend(["/app/personality.md", "personality.md"])
    return paths


def _available_personalities() -> dict[str, str]:
    found: dict[str, str] = {}
    for directory in _personality_dirs():
        try:
            for filename in os.listdir(directory):
                if not filename.endswith(".md"):
                    continue
                slug = filename[:-3].lower()
                found[slug] = slug
        except FileNotFoundError:
            continue
    for path in ["/app/personality.md", "personality.md"]:
        if os.path.exists(path):
            found.setdefault(_DEFAULT_PERSONALITY, _DEFAULT_PERSONALITY)
            break
    return dict(sorted(found.items()))


def _resolve_personality_name(requested: str | None) -> str | None:
    available = _available_personalities()
    if not available:
        return None
    if not requested:
        return _DEFAULT_PERSONALITY if _DEFAULT_PERSONALITY in available else next(iter(available))
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "", requested.lower().strip())
    if normalized in available:
        return normalized
    aliases = {
        "default": _DEFAULT_PERSONALITY,
        "hoid": "wit",
    }
    alias = aliases.get(normalized)
    if alias in available:
        return alias
    return None


def _load_personality(name: str | None = None) -> str:
    resolved = _resolve_personality_name(name)
    if not resolved:
        logger.warning("No personality prompts found — running without personality file")
        return ""
    if resolved in _personality_cache:
        return _personality_cache[resolved]
    for path in _personality_paths(resolved):
        try:
            with open(path) as f:
                personality = f.read().strip()
            _personality_cache[resolved] = personality
            logger.info(f"Loaded personality {resolved!r} from {path} ({len(personality)} chars)")
            return personality
        except FileNotFoundError:
            continue
    logger.warning(f"Personality {resolved!r} was listed but could not be loaded")
    _personality_cache[resolved] = ""
    return ""


async def _get_user_personality_name(user_id: int) -> str | None:
    saved = await db.get_user_setting(user_id, _PERSONALITY_SETTING_KEY)
    resolved = _resolve_personality_name(saved)
    if resolved:
        return resolved
    return _resolve_personality_name(None)


def _format_personality_list(current: str | None = None) -> str:
    names = list(_available_personalities())
    if not names:
        return "No personalities are configured."
    lines = ["Available personalities:"]
    for name in names:
        marker = " (current)" if name == current else ""
        lines.append(f"- {name}{marker}")
    return "\n".join(lines)


async def _personality_quip(result: str, context: str = "this", user_id: int | None = None) -> str:
    """Make a constrained LLM call for a single in-character sentence reacting to result."""
    personality_name = await _get_user_personality_name(user_id) if user_id is not None else None
    personality = _load_personality(personality_name)
    if not personality:
        return ""
    try:
        client = ollama.AsyncClient(host=Config.OLLAMA_HOST)
        resp = await client.chat(
            model=Config.OLLAMA_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        personality + "\n\n"
                        "Respond with exactly one brief in-character sentence. "
                        "Plain text only, no markdown. Do not repeat the action or data verbatim."
                    ),
                },
                {
                    "role": "user",
                    "content": f"React briefly to {context}:\n{result}",
                },
            ],
            tools=[],
        )
        if isinstance(resp, dict):
            quip = resp.get("message", {}).get("content", "").strip()
        else:
            quip = (resp.message.content or "").strip()
        return _strip_thinking(quip)
    except Exception:
        return ""


# Per-user conversation history: user_id -> list of message dicts
_history: dict[int, list[dict]] = defaultdict(list)


def _system_prompt(memories: list[dict] | None = None, personality_name: str | None = None) -> str:
    now = datetime.now(ZoneInfo(Config.TIMEZONE)).strftime("%A, %B %d %Y %H:%M %Z")
    personality = _load_personality(personality_name)
    tz = ZoneInfo(Config.TIMEZONE)
    _now = datetime.now(tz)
    today = _now.strftime("%Y-%m-%d")
    tomorrow = (_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday = (_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)).strftime("%Y-%m-%d")
    ops = f"""Today is {now} ({Config.TIMEZONE}).

DATE REFERENCE — use these exact values when constructing ISO datetimes:
- today      = {today}
- tomorrow   = {tomorrow}
- yesterday  = {yesterday}
- this year  = {_now.year}
NEVER use any other year. NEVER guess a date — always derive from the reference above.
NEVER search the web for the current date or time — it is already provided above.

You have tools for: to-do lists, reminders, web search, Home Assistant control, calendar management, and AnyList (shopping lists and meal plans).

Operational rules:
- Be concise (Telegram chat). Do NOT use Markdown formatting (no **bold**, no *italic*, no `code`). Plain text only.
- For reminders confirm the exact time back to the user.
- To snooze a reminder, call reminder_snooze(duration="..."). If the user doesn't give a duration, call reminder_snooze() with no args and ask them how long.
- For a daily briefing/summary, create a smart recurring reminder (smart=true) with an instruction like "Give me a summary of today's calendar events, current weather, and any reminders I have".
- For web searches give a 2-3 sentence summary and top 3 links.
- Never show raw JSON. Format results as readable text.
- When a tool returns a list of items (shopping list, calendar events, reminders, to-do items, meals), output each item on its own line exactly as returned. Do NOT rewrite them as a prose sentence or paragraph.

Home Assistant rules:
- Use ha_turn_on, ha_turn_off, or ha_toggle for simple on/off control — provide the exact entity_id.
- If the user gives a name but not an entity_id, make your best guess (e.g. "office light" -> entity_id="light.office").
- Use ha_get_state to check a single entity, ha_get_states with domains=["light"] to see all lights.
- Use ha_call_service for advanced control (brightness, temperature, etc).
- NEVER use search_web for Home Assistant. If entity not found, ask the user for the exact entity_id.
Calendar: use get_calendar_events(start, end) to list events. start and end are ISO 8601 dates (YYYY-MM-DD). If no date is specified, default to today. You can only READ calendar events — creating, updating, and deleting events is not supported.
AnyList rules:
- For ANY question about what's for dinner, lunch, breakfast, or what meals are planned, you MUST call anylist_get_meal_plan(start, end). Never answer a meal question without calling this tool first.
- For ANY shopping list question — including "what do I need to get at [store]", "what's on my [name] list", "what should I get from [store]" — call anylist_get_list(list_name="[store or list name]") immediately. The store name IS the list name. Never ask for clarification; never answer without calling this tool first.
- To discover available list names, call anylist_get_list() with no arguments.

Memory rules:
- When the user says "remember", "note that", or tells you a reusable fact (e.g. "my wife's calendar is Family", "the office light is light.office_main"), you MUST call the memory_save tool immediately. Do NOT just say you'll remember — call the tool.
- If the user says "whenever I send a URL/link add it to X list", call memory_save with key="url_auto_list" and value=the list name. To disable, call memory_delete with key="url_auto_list".
- ALWAYS consult the KNOWN FACTS section at the top of this prompt before answering personal questions. Never say you don't know something that appears there.
- When you apply a memory, do so silently. Don't announce it.
"""
    if memories:
        mem_lines = "\n".join(f"  {m['key']} = {m['value']}" for m in memories)
        mem_block = (
            "=== SAVED USER FACTS — READ FIRST, USE EXACTLY AS WRITTEN ===\n"
            f"{mem_lines}\n"
            "When asked about any of the above, quote the saved value exactly. "
            "Do not paraphrase, do not expand abbreviations, do not guess.\n"
            "=== END SAVED FACTS ===\n\n"
        )
    else:
        mem_block = ""

    if personality:
        return mem_block + personality + "\n\n---\n\n" + ops
    return mem_block + "You are a helpful personal assistant.\n\n" + ops


_DATE_KEYWORDS = (
    "today", "tonight", "tomorrow", "yesterday",
    "this morning", "this afternoon", "this evening", "this week",
    "next week", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
)

def _parse_reminder_request(text: str, now: datetime) -> tuple[str | None, datetime | None]:
    """
    Best-effort parse of natural-language reminder text.
    Returns (message, fire_at) or (None, None) if we can't determine either.
    """
    lower = text.lower()

    # Extract the "to <task>" portion
    msg: str | None = None
    m = re.search(r"\bto\b\s+(.+?)(?:\s+(?:in|at)\s+\d|\s*$)", lower)
    if m:
        msg = m.group(1).strip()
    if not msg:
        # Fallback: strip the preamble, use remainder as message
        m2 = re.sub(r"^(?:remind(?:\s+me)?(?:\s+to)?|set(?:\s+a)?\s+reminder(?:\s+for)?)\s*", "", lower, flags=re.IGNORECASE).strip()
        if m2:
            msg = m2

    # Parse relative offset: "in X minutes/hours/days"
    m = re.search(r"\bin\s+(\d+)\s+(min(?:utes?|s)?|hr?s?|hours?|days?)\b", lower)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if "hour" in unit or unit.startswith("hr"):
            fire_at = now + timedelta(hours=n)
        elif "day" in unit:
            fire_at = now + timedelta(days=n)
        else:
            fire_at = now + timedelta(minutes=n)
        return msg, fire_at

    # Parse clock time: "at HH:MM [am/pm]" with optional "tomorrow"
    m = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", lower)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3)
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        fire_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if "tomorrow" in lower:
            fire_at += timedelta(days=1)
        elif fire_at <= now:
            fire_at += timedelta(days=1)
        return msg, fire_at

    return msg, None


_HA_ACTION_RE = re.compile(
    r'\b(turn\s+on|turn\s+off|toggle|switch\s+on|switch\s+off)\s+(?:the\s+)?(.+?)(?:\s*[,.]|$)',
    re.IGNORECASE,
)
_HA_ACTION_TOOL = {
    "turnon": "ha_turn_on", "switchon": "ha_turn_on",
    "turnoff": "ha_turn_off", "switchoff": "ha_turn_off",
    "toggle": "ha_toggle",
}


def _parse_ha_request(text: str, memories: list[dict]) -> tuple[str | None, str | None]:
    """Parse 'turn on/off/toggle the X' and return (tool_name, entity_id) or (None, None)."""
    m = _HA_ACTION_RE.search(text)
    if not m:
        return None, None
    action_key = re.sub(r"\s+", "", m.group(1).lower())
    tool_name = _HA_ACTION_TOOL.get(action_key)
    if not tool_name:
        return None, None
    thing = m.group(2).strip().lower()
    # Search memories for an entity_id for this thing
    mem_lookup = {mem["key"].lower(): mem["value"] for mem in memories}
    entity_id = None
    for key_suffix in (f"{thing} entity_id", thing):
        if key_suffix in mem_lookup and "." in mem_lookup[key_suffix]:
            entity_id = mem_lookup[key_suffix]
            break
    if not entity_id:
        thing_words = {w for w in thing.split() if w not in {"the", "a", "an"}}
        for key, value in mem_lookup.items():
            if "." not in value:
                continue
            key_words = {w for w in re.sub(r"entity[_\s]?id", "", key, flags=re.IGNORECASE).split()
                         if w not in {"the", "for", "of", ""}}
            if thing_words and key_words and (thing_words <= key_words or key_words <= thing_words):
                entity_id = value
                break
    return tool_name, entity_id


def _inject_date_context(message: str) -> str:
    """Replace relative date keywords with ISO dates so the model never has to compute them."""
    lower = message.lower()
    if not any(w in lower for w in _DATE_KEYWORDS):
        return message
    tz = ZoneInfo(Config.TIMEZONE)
    now = datetime.now(tz)

    # Annotate keywords with their ISO date in parentheses — preserves grammar while grounding dates
    direct = {
        "tonight":   now.strftime("%Y-%m-%d"),
        "today":     now.strftime("%Y-%m-%d"),
        "tomorrow":  (now + timedelta(days=1)).strftime("%Y-%m-%d"),
        "yesterday": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
    }
    result = message
    for keyword, date_str in direct.items():
        result = re.sub(
            r"\b" + re.escape(keyword) + r"\b",
            f"{keyword} ({date_str})",
            result,
            flags=re.IGNORECASE,
        )

    # Annotate "this week" and "next week" with Sunday-Saturday date ranges (US convention)
    days_since_sunday = (now.weekday() + 1) % 7  # Mon=0 → 1 day since Sun, Sun=6 → 0
    this_week_sun = (now - timedelta(days=days_since_sunday)).replace(hour=0, minute=0, second=0, microsecond=0)
    week_phrases = {
        "this week": (this_week_sun, this_week_sun + timedelta(days=6)),
        "next week": (this_week_sun + timedelta(days=7), this_week_sun + timedelta(days=13)),
    }
    for phrase, (wstart, wend) in week_phrases.items():
        result = re.sub(
            r"\b" + re.escape(phrase) + r"\b",
            f"{phrase} ({wstart.strftime('%Y-%m-%d')} to {wend.strftime('%Y-%m-%d')})",
            result,
            flags=re.IGNORECASE,
        )

    # Annotate weekday names the same way
    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    weekday_dates = {}
    for i, name in enumerate(day_names):
        days_ahead = (i - now.weekday()) % 7 or 7
        weekday_dates[name] = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    for name, date_str in weekday_dates.items():
        result = re.sub(
            r"\b" + name + r"\b",
            f"{name} ({date_str})",
            result,
            flags=re.IGNORECASE,
        )

    return result


_ISO_DATE_PART_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_US_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_DATE_FIELDS = {"start", "end", "new_start", "new_end", "fire_at",
                "start_date", "end_date", "new_start_date", "new_end_date",
                "start_time", "end_time", "date"}  # catches full-datetime values passed in *_time fields


def _extract_date_parts(text: str) -> list[str]:
    """Return unique YYYY-MM-DD values found in text, sorted chronologically."""
    seen: set[str] = set()
    for m in _ISO_DATE_PART_RE.finditer(text):
        seen.add(m.group(1))
    for m in _US_DATE_RE.finditer(text):
        iso = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        seen.add(iso)
    return sorted(seen)


def _closest_date(target: str, candidates: list[str]) -> str:
    """Return the candidate date closest in calendar distance to target."""
    from datetime import date as _date
    try:
        t = _date.fromisoformat(target)
        best, best_diff = candidates[0], None
        for c in candidates:
            try:
                diff = abs((_date.fromisoformat(c) - t).days)
                if best_diff is None or diff < best_diff:
                    best, best_diff = c, diff
            except ValueError:
                pass
        return best
    except ValueError:
        return candidates[0]


def _enforce_grounded_dates(tool_calls: list[dict], grounded_dates: list[str]) -> None:
    """Replace any date in tool args that wasn't grounded by _inject_date_context."""
    if not grounded_dates:
        return
    for tc in tool_calls:
        args = tc.get("arguments", {})
        for field in _DATE_FIELDS:
            val = args.get(field)
            if not isinstance(val, str):
                continue
            m = _ISO_DATE_PART_RE.match(val)
            if not m:
                continue
            date_part = m.group(1)
            if date_part in grounded_dates:
                continue  # already correct
            replacement = _closest_date(date_part, grounded_dates) if len(grounded_dates) > 1 else grounded_dates[0]
            corrected = val.replace(date_part, replacement, 1)
            logger.warning(f"Grounded date override: {field} {val!r} -> {corrected!r}")
            args[field] = corrected


def _trim_history(user_id: int):
    """Keep only the last N messages, always keeping pairs intact."""
    window = min(Config.HISTORY_WINDOW, 6)
    history = _history[user_id]
    if len(history) > window:
        # Always keep an even number to avoid orphaned tool messages
        trimmed = history[-window:]
        # Don't start with a tool message
        while trimmed and trimmed[0].get("role") == "tool":
            trimmed = trimmed[1:]
        _history[user_id] = trimmed


def clear_history(user_id: int):
    _history[user_id] = []


# Matches: "remember/note that <KEY> is [called] <VALUE>"
_MEMORY_RE = re.compile(
    r'(?:remember|note|save|store)\s+(?:that\s+)?["\']?(.+?)["\']?\s+'
    r'(?:is(?:\s+called)?|means?|refers?\s+to|=)\s+["\']?(.+?)["\']?\s*$',
    re.IGNORECASE,
)

# Matches: "turn off/on the THING, the entity_id ... is ENTITY" or "THING entity_id is ENTITY"
_ENTITY_ID_RE = re.compile(
    r'(?:turn\s+(?:on|off)|toggle)\s+(?:the\s+)?(?P<thing>[\w\s]+?)\s*[,.].*?'
    r'entity[_\s]id\s+(?:for\s+\S+\s+)?is\s+["\']?(?P<entity>[\w]+\.[\w]+)["\']?'
    r'|(?:the\s+)?entity[_\s]id\s+for\s+(?:the\s+)?["\']?(?P<thing2>[\w\s]+?)["\']?\s+is\s+["\']?(?P<entity2>[\w]+\.[\w]+)["\']?',
    re.IGNORECASE,
)


async def _maybe_save_memory(user_id: int, user_message: str) -> str | None:
    """Fallback: parse fact-stating messages and save to memory."""
    # Pattern: "remember/note that X is Y"
    m = _MEMORY_RE.search(user_message)
    if m:
        key = m.group(1).strip().strip('"\'')
        value = m.group(2).strip().strip('"\'')
        if key and value:
            await db.save_memory(user_id, key, value)
            logger.info(f"Fallback memory save: {key!r} = {value!r}")
            return f"{key} = {value}"

    # Pattern: entity_id facts — "turn off the office light, the entity_id is light.office"
    m = _ENTITY_ID_RE.search(user_message)
    if m:
        thing = (m.group("thing") or m.group("thing2") or "").strip()
        entity = (m.group("entity") or m.group("entity2") or "").strip()
        if thing and entity:
            key = f"{thing} entity_id"
            await db.save_memory(user_id, key, entity)
            logger.info(f"Fallback entity_id save: {key!r} = {entity!r}")
            return f"{key} = {entity}"

    return None


def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks from model output."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.strip()


_TEXT_TOOL_RE = re.compile(
    r"<(?:execute_tool|tool_call|tool_use)>\s*([\w]+)\s*\(([\s\S]*?)\)\s*</(?:execute_tool|tool_call|tool_use)>",
    re.IGNORECASE,
)

# Bare funcname(...) with at least one key=value arg — matched against known names only
_BARE_TOOL_RE = re.compile(r"\b([\w]+)\s*\(([^)]*\w\s*=\s*[^)]+)\)", re.IGNORECASE)

# Bare funcname() with NO args — matched against known names only
_BARE_NO_ARG_TOOL_RE = re.compile(r"\b([\w]+)\s*\(\s*\)", re.IGNORECASE)

_KWARG_RE = re.compile(
    r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|(true|false|null|-?\d+(?:\.\d+)?))',
    re.IGNORECASE,
)


def _parse_kwargs_string(args_str: str) -> dict:
    args = {}
    for m in _KWARG_RE.finditer(args_str):
        key = m.group(1)
        if m.group(2) is not None:
            val: object = m.group(2)
        elif m.group(3) is not None:
            val = m.group(3)
        else:
            raw = m.group(4)
            if raw.lower() == "true":
                val = True
            elif raw.lower() == "false":
                val = False
            elif raw.lower() == "null":
                val = None
            else:
                try:
                    val = int(raw)
                except ValueError:
                    try:
                        val = float(raw)
                    except ValueError:
                        val = raw
        args[key] = val
    return args


def _parse_text_tool_calls(content: str) -> list[dict]:
    """Extract tool calls the model wrote as text instead of native tool-call mechanism."""
    results = []

    # Pattern 1: <execute_tool>name(...)</execute_tool> (and similar XML wrappers)
    for m in _TEXT_TOOL_RE.finditer(content):
        name = m.group(1).lower()
        args = _parse_kwargs_string(m.group(2))
        results.append({"name": name, "arguments": args, "id": name})

    if results:
        return results

    # Pattern 2: JSON array of tool calls — strip markdown code fences first
    stripped = re.sub(r"```(?:json)?\s*", "", content).replace("```", "").strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = (item.get("tool_name") or item.get("function") or
                        item.get("name") or item.get("tool") or "")
                args = (item.get("parameters") or item.get("arguments") or
                        item.get("args") or item.get("params") or {})
                if name and isinstance(args, dict):
                    results.append({"name": str(name).lower(), "arguments": args, "id": str(name).lower()})
        except (json.JSONDecodeError, TypeError):
            pass

    if results:
        return results

    # Pattern 3: bare funcname(key=value, ...) — only for known tool/alias names
    for m in _BARE_TOOL_RE.finditer(content):
        name = m.group(1).lower()
        if name in _KNOWN_TOOLS:
            args = _parse_kwargs_string(m.group(2))
            results.append({"name": name, "arguments": args, "id": name})

    if results:
        return results

    # Pattern 4: bare funcname() with no args — only for known tool/alias names
    for m in _BARE_NO_ARG_TOOL_RE.finditer(content):
        name = m.group(1).lower()
        if name in _KNOWN_TOOLS:
            results.append({"name": name, "arguments": {}, "id": name})

    if results:
        return results

    # Pattern 5: known tool name on its own line followed by a JSON block
    # e.g. "memory\n[{"name": "...", "value": "..."}]"
    for m in re.finditer(r'^\s*(\w+)\s*\n\s*([{\[])', content, re.MULTILINE):
        name = m.group(1).lower()
        resolved = _TOOL_ALIASES.get(name, name)
        if resolved not in {td["function"]["name"] for td in TOOL_DEFINITIONS}:
            continue
        try:
            fragment = content[m.start(2):].strip()
            data = json.loads(fragment)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                args = (item.get("parameters") or item.get("arguments") or item.get("args") or {})
                if not args:
                    # Item itself is the args (e.g. {"name": "x", "value": "y"})
                    args = {k: v for k, v in item.items()
                            if k not in ("tool_name", "function", "tool")}
                # Normalise "name" → "key" for memory_save
                if resolved == "memory_save" and "name" in args and "key" not in args:
                    args["key"] = args.pop("name")
                if args:
                    results.append({"name": resolved, "arguments": args, "id": resolved})
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    if results:
        return results

    # Pattern 7: "ha_call:..." hallucinated format with various namespace styles
    # e.g. "ha_call:home_assistant:turn_on{entity_id:light.office}"
    # e.g. "ha_call: /api/turn_off_light{device_id: "office_light"}"
    _HA_CALL_COLON_RE = re.compile(
        r'\bha[_\s]?call\s*:\s*(?:[\w/_]+\s*:\s*)?'   # optional namespace or /path/
        r'(?P<action>turn[_\s]?off|turn[_\s]?on|toggle)\w*\s*'
        r'\{(?P<args>[^}]+)\}',
        re.IGNORECASE,
    )
    for m in _HA_CALL_COLON_RE.finditer(content):
        action_key = re.sub(r"[_\s]", "", m.group("action").lower())
        tool_name = _HA_ACTION_TOOL.get(action_key)
        if not tool_name:
            continue
        raw_args = m.group("args")
        args = {}
        for pair in re.split(r"[,;]", raw_args):
            if ":" in pair:
                k, _, v = pair.partition(":")
                args[k.strip().strip("\"'")] = v.strip().strip("\"'")
            elif "=" in pair:
                k, _, v = pair.partition("=")
                args[k.strip().strip("\"'")] = v.strip().strip("\"'")
        if args:
            results.append({"name": tool_name, "arguments": args, "id": tool_name})

    if results:
        return results

    # Pattern 6: hallucinated HA command syntax
    # e.g. "hacommand: home.turnofflight lightid: light.office"
    _HA_HALLUC_RE = re.compile(
        r'\b(?:ha[_\s]?command|homeassistant[_\s]?(?:command|cmd)?)\s*:\s*'
        r'(?:home\.)?(?P<action>turn[_\s]?off|turn[_\s]?on|toggle)\w*\s+'
        r'(?:light[_\s]?id|entity[_\s]?id|lightid|entityid|entity|id)\s*:\s*(?P<entity>[\w.]+)',
        re.IGNORECASE,
    )
    _HA_ACTION_MAP = {
        "turnoff": "ha_turn_off", "turn_off": "ha_turn_off",
        "turnon": "ha_turn_on", "turn_on": "ha_turn_on",
        "toggle": "ha_toggle",
    }
    for m in _HA_HALLUC_RE.finditer(content):
        action_raw = re.sub(r'[_\s]', '', m.group("action").lower())
        # Strip trailing "light" suffix (e.g. "turnofflight" → "turnoff")
        for suffix in ("light", "device", "entity"):
            if action_raw.endswith(suffix):
                action_raw = action_raw[: -len(suffix)]
        tool_name = _HA_ACTION_MAP.get(action_raw)
        if tool_name:
            results.append({
                "name": tool_name,
                "arguments": {"entity_id": m.group("entity")},
                "id": tool_name,
            })

    return results


def _strip_text_tool_calls(content: str) -> str:
    stripped = _TEXT_TOOL_RE.sub("", content).strip()
    # Also strip bare no-arg tool calls that were parsed
    stripped = _BARE_NO_ARG_TOOL_RE.sub("", stripped).strip()
    return stripped


def _extract_message(response) -> tuple[str, list[dict]]:
    """
    Normalise the ollama response into (content, tool_calls).
    Handles both dict responses (current lib) and object responses (older lib).
    Also handles thinking models that return a 'thinking' field or <think> tags.
    tool_calls is a list of {"name": str, "arguments": dict, "id": str}
    """
    if isinstance(response, dict):
        msg = response.get("message", {})
        content = msg.get("content") or ""
        raw_calls = msg.get("tool_calls") or []
    else:
        msg = response.message
        content = msg.content or ""
        raw_calls = msg.tool_calls or []

    # Strip <think> blocks from thinking models (Gemma4, Qwen3, etc.)
    content = _strip_thinking(content)

    tool_calls = []
    for tc in raw_calls:
        if isinstance(tc, dict):
            fn = tc.get("function", {})
            name = fn.get("name", "")
            arguments = fn.get("arguments", {})
            call_id = tc.get("id", name)
        else:
            name = tc.function.name
            arguments = tc.function.arguments
            call_id = getattr(tc, "id", name)

        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}

        tool_calls.append({"name": name, "arguments": arguments, "id": call_id})

    return content, tool_calls


def _maybe_inject_entity_hint(search_result: str, user_message: str) -> str:
    """
    If search_entities returned results and the user wants to control a device,
    extract the top entity_id and tell the model exactly what to do next.
    """
    control_words = ("turn off", "turn on", "toggle", "switch off", "switch on",
                     "dim", "brighten", "set", "enable", "disable")
    user_lower = user_message.lower()
    is_control = any(w in user_lower for w in control_words)
    if not is_control:
        return search_result

    try:
        data = json.loads(search_result)
        results = data.get("data", {}).get("results", [])
        if not results:
            return search_result
        entity_id = results[0].get("entity_id", "")
        domain = entity_id.split(".")[0] if "." in entity_id else ""
        if not domain:
            return search_result

        if any(w in user_lower for w in ("turn off", "switch off", "disable")):
            service = "turn_off"
        elif any(w in user_lower for w in ("turn on", "switch on", "enable")):
            service = "turn_on"
        else:
            service = "toggle"

        hint = (
            f"\n\nIMPORTANT: Found entity '{entity_id}'. "
            f"You MUST now call ha__ha_call_service with: "
            f"domain=\"{domain}\", service=\"{service}\", entity_id=\"{entity_id}\". "
            f"Do NOT call any other tool first."
        )
        return search_result + hint
    except Exception:
        return search_result


async def _find_todo_item_by_name(user_id: int, list_name: str, item_query: str) -> tuple[int | None, str | None]:
    """Find a todo item in list_name that best matches item_query. Returns (item_id, content) or (None, None)."""
    import difflib
    import re as _re
    list_name = _re.sub(r"\s+lists?$", "", list_name, flags=_re.IGNORECASE).strip()
    try:
        items = await db.get_todo_items(user_id, list_name)
    except Exception:
        return None, None
    if not items:
        return None, None
    query_lower = item_query.lower().strip()
    contents_lower = [item["content"].lower() for item in items]

    # Exact match
    for item in items:
        if item["content"].lower() == query_lower:
            return item["id"], item["content"]

    # URL normalization: strip trailing slashes before comparison
    query_norm = query_lower.rstrip("/")
    for item in items:
        if item["content"].lower().rstrip("/") == query_norm:
            return item["id"], item["content"]

    # Substring match
    for item in items:
        c = item["content"].lower()
        if query_lower in c or c in query_lower:
            return item["id"], item["content"]

    # Word/fragment match — handle URL references like "the selfh.st link"
    stop_words = {"the", "a", "an", "my", "i", "have", "link", "links", "url", "urls"}
    query_words = set(query_lower.split()) - stop_words
    _url_domain_re = _re.compile(r'https?://([^/\s]+)')
    best_item, best_score = None, 0
    for item in items:
        content_lower = item["content"].lower()
        content_words = set(content_lower.split())
        score = 0
        for w in query_words:
            if "." in w or "/" in w:
                # URL fragment: substring search in full content
                if w in content_lower:
                    score += 2
                else:
                    # Fuzzy match against URL domain
                    dm = _url_domain_re.search(content_lower)
                    if dm and difflib.SequenceMatcher(None, w, dm.group(1)).ratio() >= 0.7:
                        score += 1
            elif w in content_words:
                score += 1
        if score > best_score:
            best_score, best_item = score, item

    if best_item and best_score > 0:
        return best_item["id"], best_item["content"]

    close = difflib.get_close_matches(query_lower, contents_lower, n=1, cutoff=0.5)
    if close:
        for item in items:
            if item["content"].lower() == close[0]:
                return item["id"], item["content"]
    return None, None


async def chat(user_id: int, user_message: str) -> str:
    """
    Process a user message through the Ollama agent loop.
    Returns the assistant's final reply as a string.
    """
    # Normalize Telegram smart/curly quotes → ASCII so all regex patterns match cleanly
    user_message = (user_message
        .replace("“", '"').replace("”", '"')
        .replace("‘", "'").replace("’", "'"))
    logger.info(f"chat() called for user {user_id}: {user_message[:80]!r}")
    grounded = _inject_date_context(user_message)
    grounded_dates = _extract_date_parts(grounded)
    _history[user_id].append({"role": "user", "content": grounded})
    _trim_history(user_id)
    _memory_saved = False

    memories = await db.get_memories(user_id)
    all_tools = get_active_tool_definitions()
    personality_name = await _get_user_personality_name(user_id)

    # Personality management is deterministic bot configuration, not model memory.
    _PERSONALITY_LIST_RE = re.compile(
        r"\b(?:list|show)\b.{0,40}\bpersonalit(?:y|ies)\b|"
        r"\b(?:what|which)\s+personalit(?:y|ies)\s+(?:are\s+)?(?:available|configured|installed)\b",
        re.IGNORECASE,
    )
    _PERSONALITY_CURRENT_RE = re.compile(
        r"\b(?:current|active)\s+personalit(?:y|ies)\b|"
        r"\bwhat\s+personalit(?:y|ies)\s+(?:are\s+you|am\s+i)\s+using\b",
        re.IGNORECASE,
    )
    _PERSONALITY_SWITCH_RE = re.compile(
        r"\b(?:switch|change|set|use)\s+(?:my\s+|the\s+|your\s+)?personality\s+"
        r"(?:to|as)?\s*[\"']?([A-Za-z0-9_-]{2,40})[\"']?\s*$|"
        r"\b(?:switch|change|set|use)\s+(?:to\s+)?[\"']?([A-Za-z0-9_-]{2,40})[\"']?\s+personality\s*$|"
        r"\b(?:be|act\s+as)\s+[\"']?([A-Za-z0-9_-]{2,40})[\"']?\s+personality\s*$",
        re.IGNORECASE,
    )
    if _PERSONALITY_LIST_RE.search(user_message):
        reply = _format_personality_list(personality_name)
        _history[user_id].append({"role": "assistant", "content": reply})
        return reply
    if _PERSONALITY_CURRENT_RE.search(user_message):
        reply = f"Current personality: {personality_name or 'none'}."
        _history[user_id].append({"role": "assistant", "content": reply})
        return reply
    _personality_switch = _PERSONALITY_SWITCH_RE.search(user_message)
    if _personality_switch and "personality" in user_message.lower():
        requested = next((g for g in _personality_switch.groups() if g), "").strip()
        resolved = _resolve_personality_name(requested)
        if not resolved:
            reply = f"No personality named '{requested}' found.\n\n{_format_personality_list(personality_name)}"
        else:
            await db.save_user_setting(user_id, _PERSONALITY_SETTING_KEY, resolved)
            personality_name = resolved
            clear_history(user_id)
            reply = f"Personality switched to {resolved}."
        _history[user_id].append({"role": "assistant", "content": reply})
        return reply

    # URL auto-add: if every non-empty line is a URL and url_auto_list is set in memory, add all.
    _URL_LINE_RE = re.compile(r'^\s*https?://\S+\s*$', re.IGNORECASE)
    _msg_lines = [l for l in user_message.splitlines() if l.strip()]
    if _msg_lines and all(_URL_LINE_RE.match(l) for l in _msg_lines):
        _mem_lookup_url = {m["key"].lower(): m["value"] for m in memories}
        _auto_list = _mem_lookup_url.get("url_auto_list")
        if _auto_list:
            _urls = [l.strip() for l in _msg_lines]
            logger.info(f"URL auto-add intercept: list={_auto_list!r} count={len(_urls)}")
            for _url in _urls:
                await handle_tool_call("todo_add_item", {"list_name": _auto_list, "content": _url}, user_id)
            reply = f"Added {len(_urls)} link{'s' if len(_urls) != 1 else ''} to **{_auto_list.title()}**."
            _history[user_id].append({"role": "assistant", "content": reply})
            return reply

    # Python-level intercept: answer simple "what is my X" questions directly from memory
    # so the model can't hallucinate/expand saved values.
    if memories:
        _mem_lookup = {m["key"].lower(): m["value"] for m in memories}
        _recall_match = re.search(
            r"\bwhat(?:'s| is)\s+my\s+(.+?)(?:\?|$)", user_message, re.IGNORECASE
        )
        if _recall_match:
            queried_key = _recall_match.group(1).strip().lower()
            # Try exact match, then prefix match
            direct_val = _mem_lookup.get(queried_key) or next(
                (v for k, v in _mem_lookup.items() if queried_key in k or k in queried_key), None
            )
            if direct_val:
                reply = direct_val
                _history[user_id].append({"role": "assistant", "content": reply})
                return reply

    # Pre-model intercepts: bypass the model entirely for queries we can always handle
    # deterministically. This prevents hallucination and prompt-injection contamination.

    # URL auto-add rule: "whenever I send a URL/link, add it to X list"
    _URL_RULE_RE = re.compile(
        r"\b(?:whenever|when|any\s+time|if)\b.{0,40}?\b(?:send|paste|share)\b.{0,20}?"
        r"\b(?:just\s+)?(?:a\s+)?(?:link|url)\b.{0,60}?\badd\s+it\s+to\b.{0,30}?"
        r"\b(?:(?:the|my)\s+)?[\"']?([A-Za-z][\w\s]{1,30}?)[\"']?\s*list\b",
        re.IGNORECASE,
    )
    _url_rule_match = _URL_RULE_RE.search(user_message)
    if _url_rule_match:
        _target_list = _url_rule_match.group(1).strip().strip("\"'“”‘’")
        await db.save_memory(user_id, "url_auto_list", _target_list)
        logger.info(f"URL auto-add rule saved: list={_target_list!r}")
        reply = f"Got it. Any URL you send me on its own will automatically be added to the '{_target_list}' list."
        _history[user_id].append({"role": "assistant", "content": reply})
        return reply

    # Reminder list
    _LIST_REMINDER_WORDS = ("what reminders", "list reminders", "show reminders",
                            "my reminders", "reminders do i have", "any reminders")
    if any(w in user_message.lower() for w in _LIST_REMINDER_WORDS):
        logger.info("Pre-model reminder list intercept")
        fallback_result = await handle_tool_call("reminder_list", {}, user_id)
        _history[user_id].append({"role": "assistant", "content": fallback_result})
        return fallback_result

    # Reminder delete by name
    _DELETE_REMINDER_RE = re.compile(
        r"\b(?:delete|remove|cancel|dismiss|clear|get\s+rid\s+of)\b.{0,30}?\breminder\b",
        re.IGNORECASE,
    )
    if _DELETE_REMINDER_RE.search(user_message):
        reminders = await db.get_reminders(user_id)
        if reminders:
            # If only one reminder exists, delete it directly
            if len(reminders) == 1:
                target = reminders[0]
            else:
                # Fuzzy-match: find the reminder whose message best overlaps the user's words
                msg_lower = user_message.lower()
                best, best_score = None, 0
                for r in reminders:
                    score = sum(1 for w in r["message"].lower().split() if w in msg_lower)
                    if score > best_score:
                        best_score, best = score, r
                target = best if best_score > 0 else None
            if target:
                logger.info(f"Pre-model reminder delete intercept: id={target['id']} msg={target['message']!r}")
                result = await handle_tool_call("reminder_delete", {"reminder_id": target["id"]}, user_id)
                quip = await _personality_quip(result, "deleting this reminder", user_id)
                reply = quip if quip else result
                _history[user_id].append({"role": "assistant", "content": reply})
                return reply

    # Reminder snooze
    _SNOOZE_RE = re.compile(r"\b(?:snooze|delay|postpone|remind\s+me\s+again)\b", re.IGNORECASE)
    if _SNOOZE_RE.search(user_message):
        _dur_match = re.search(
            r"(?:for\s+)?(\d+\s*(?:min(?:utes?|s)?|hr?s?|hours?|days?))",
            user_message, re.IGNORECASE,
        )
        if _dur_match:
            _duration_str = _dur_match.group(1).strip()
            _last_id = sched.get_last_fired_reminder_id(user_id)
            if _last_id:
                logger.info(f"Pre-model snooze intercept: id={_last_id}, duration={_duration_str!r}")
                result = await handle_tool_call("reminder_snooze", {"duration": _duration_str, "reminder_id": _last_id}, user_id)
                quip = await _personality_quip(result, "this reminder snooze", user_id)
                reply = quip if quip else result
                _history[user_id].append({"role": "assistant", "content": reply})
                return reply

    # HA turn on/off/toggle — only if we can fully resolve the entity from memory
    if Config.HA_URL:
        ha_tool, ha_entity = _parse_ha_request(user_message, memories)
        if ha_tool and ha_entity:
            logger.info(f"Pre-model HA intercept: {ha_tool}(entity_id={ha_entity!r})")
            fallback_result = await handle_tool_call(ha_tool, {"entity_id": ha_entity}, user_id)
            quip = await _personality_quip(fallback_result, "this Home Assistant action", user_id)
            reply = quip if quip else fallback_result
            _history[user_id].append({"role": "assistant", "content": reply})
            return reply

    # Calendar
    if Config.CALDAV_URL and Config.CALDAV_USERNAME and Config.CALDAV_PASSWORD:
        _CAL_WORDS = ("calendar", "schedule", "events", "appointments", "agenda")
        if any(w in user_message.lower() for w in _CAL_WORDS):
            today_str = datetime.now(ZoneInfo(Config.TIMEZONE)).strftime("%Y-%m-%d")
            start_str = grounded_dates[0] if grounded_dates else today_str
            end_str = grounded_dates[-1] if grounded_dates else today_str
            logger.info(f"Pre-model calendar intercept: {start_str} to {end_str}")
            cal_result = await handle_tool_call("get_calendar_events", {"start": start_str, "end": end_str}, user_id)
            reply = "**\U0001f4c5 Calendar events:**\n\n" + cal_result
            _history[user_id].append({"role": "assistant", "content": reply})
            return reply

    # Weather
    if Config.HA_WEATHER_ENTITY:
        _WEATHER_WORDS = ("weather", "temperature", "forecast", "outside", "rain", "sunny", "cold", "hot", "humid")
        if any(w in user_message.lower() for w in _WEATHER_WORDS):
            logger.info("Pre-model weather intercept")
            fallback_result = await handle_tool_call("ha_get_weather", {}, user_id)
            quip = await _personality_quip(fallback_result, "the current weather conditions", user_id)
            reply = fallback_result + ("\n\n" + quip if quip else "")
            _history[user_id].append({"role": "assistant", "content": reply})
            return reply

    # Helper: strip ASCII and Unicode curly/smart quotes that Telegram inserts
    _SMART_QUOTES = "\u201c\u201d\u2018\u2019"

    def _sq(s):
        return s.strip().strip("\'\"" + _SMART_QUOTES)

    # To-do: create list
    _CREATE_LIST_RE = re.compile(
        r"\b(?:create|make|start)\s+(?:a\s+)?(?:new\s+)?(?:to-?do\s+|task\s+)?list\s+(?:called|named|titled|for)?\s*[\"\']?([^\"\'?\n]{2,40}?)[\"\']?\s*\??$",
        re.IGNORECASE,
    )
    _cl_match = _CREATE_LIST_RE.search(user_message)
    if _cl_match:
        _lname = _sq(_cl_match.group(1))
        logger.info(f"Pre-model todo create list intercept: name={_lname!r}")
        result = await handle_tool_call("todo_create_list", {"name": _lname}, user_id)
        _history[user_id].append({"role": "assistant", "content": result})
        return result

    # To-do: clear all items — must come before single-item delete so "remove both/all items"
    # isn't mistaken for an item named "both items".
    _CLEAR_LIST_QUANT_RE = re.compile(
        r"\b(?:remove|delete|erase|clear|wipe)\s+"
        r"(?:all|both|everything)(?:\s+(?:of\s+(?:them|it)|(?:the\s+)?items?|of\s+the\s+items?))?"
        r"\s+(?:from|on|in)\s+(?:(?:the|my)\s+)?[\"']?"
        r"([A-Za-z][\w\s]{1,30}?)[\"']?\s*(?:list\b)?\s*\??$",
        re.IGNORECASE,
    )
    _CLEAR_LIST_VERB_RE = re.compile(
        r"\bclear\s+(?:(?:the|my)\s+)?[\"']?"
        r"([A-Za-z][\w\s]{1,30}?)[\"']?\s+list\b\s*\??$",
        re.IGNORECASE,
    )
    _clr_match = _CLEAR_LIST_QUANT_RE.search(user_message) or _CLEAR_LIST_VERB_RE.search(user_message)
    if _clr_match:
        _lname = _sq(_clr_match.group(1)).strip()
        logger.info(f"Pre-model todo clear list intercept: list={_lname!r}")
        result = await handle_tool_call("todo_clear_list", {"list_name": _lname}, user_id)
        _history[user_id].append({"role": "assistant", "content": result})
        return result

    # To-do: delete item from list — "delete/remove X from Y", "remove X from Y list"
    # Must come before the delete-list intercept to avoid "remove X from Y list" being
    # misread as a list named "X from Y".
    _DEL_ITEM_RE = re.compile(
        r"\b(?:delete|remove|erase|cross\s+off)\s+(?:the\s+)?(?:task|item|entry|to-?do)?\s*"
        r"[\"\'“”‘’]?(.+?)[\"\'“”‘’]?"
        r"\s+from\s+(?:(?:the|my)\s+)?[\"\'“”‘’]?"
        r"([A-Za-z][\w\s]{1,30}?)[\"\'“”‘’]?\s*(?:list\b)?\s*\??$",
        re.IGNORECASE,
    )
    _di_match = _DEL_ITEM_RE.search(user_message)
    if _di_match:
        _item_q = _sq(_di_match.group(1))
        _list_q = _sq(_di_match.group(2)).strip()
        logger.info(f"Pre-model todo delete item intercept: item={_item_q!r} list={_list_q!r}")
        _item_id, _matched = await _find_todo_item_by_name(user_id, _list_q, _item_q)
        if _item_id is not None:
            await handle_tool_call("todo_delete_item", {"item_id": _item_id}, user_id)
            reply = f"Deleted {_matched} from **{_list_q.title()}**."
        else:
            # Check if the item lives in AnyList (read-only) so we can give a useful message
            reply = f"Couldn't find {_item_q} on **{_list_q.title()}**."
            if Config.ANYLIST_EMAIL:
                _al_check = await handle_tool_call("anylist_get_list", {"list_name": _list_q}, user_id)
                if not _al_check.startswith("Tool error:") and _item_q.rstrip("/").lower() in _al_check.lower():
                    reply = f"That item is in your AnyList — remove it from the AnyList app directly."
        _history[user_id].append({"role": "assistant", "content": reply})
        return reply

    # To-do: delete list
    _DEL_LIST_RE1 = re.compile(
        r"\b(?:delete|remove|drop)\s+(?:the\s+)?list\s+(?:called|named|titled)?\s*[\"\']?([^\"\'?\n]{2,40}?)[\"\']?\s*\??$",
        re.IGNORECASE,
    )
    _DEL_LIST_RE2 = re.compile(
        r"\b(?:delete|remove|drop)\s+(?:the\s+)?[\"\']?([^\"\'?\n]{2,40}?)[\"\']?\s+list\b",
        re.IGNORECASE,
    )
    _dl_match = _DEL_LIST_RE1.search(user_message) or _DEL_LIST_RE2.search(user_message)
    if _dl_match:
        _lname = _sq(_dl_match.group(1))
        logger.info(f"Pre-model todo delete list intercept: name={_lname!r}")
        result = await handle_tool_call("todo_delete_list", {"name": _lname}, user_id)
        _history[user_id].append({"role": "assistant", "content": result})
        return result

    # To-do: add item to list (always internal todo - AnyList is read-only from the bot)
    # Three patterns in order: "add X to list called Y", "add X to Y list", "add X to Y"
    _ADD_1_RE = re.compile(
        r"\badd\s+[\"\']?(.+?)[\"\']?\s+to\s+(?:the\s+)?list\s+(?:called|named|titled)\s+[\"\']?([A-Za-z][\w\s]{1,30}?)[\"\']?\s*\??$",
        re.IGNORECASE,
    )
    _ADD_2_RE = re.compile(
        r"\badd\s+[\"\']?(.+?)[\"\']?\s+to\s+(?:(?:the|my)\s+)?([A-Za-z][\w\s]{1,30}?)\s+list\b",
        re.IGNORECASE,
    )
    _ADD_3_RE = re.compile(
        r"\badd\s+[\"\']?(.+?)[\"\']?\s+to\s+(?:(?:the|my)\s+)?([A-Za-z][\w\s]{1,30}?)\s*\??$",
        re.IGNORECASE,
    )
    _ai_match = _ADD_1_RE.search(user_message) or _ADD_2_RE.search(user_message) or _ADD_3_RE.search(user_message)
    if _ai_match:
        _item_content = _sq(_ai_match.group(1))
        _todo_list = _sq(_ai_match.group(2)).strip()
        logger.info(f"Pre-model todo add item intercept: content={_item_content!r} list={_todo_list!r}")
        result = await handle_tool_call("todo_add_item", {"list_name": _todo_list, "content": _item_content}, user_id)
        if "not found" in result.lower() and Config.ANYLIST_EMAIL:
            result += "\n\nNote: AnyList shopping lists (Groceries, Target, etc.) are read-only - add items directly in the AnyList app."
        _history[user_id].append({"role": "assistant", "content": result})
        return result

    # List read: try AnyList first (if configured), fall back to internal todo
    _STORE_RE_PRE = re.compile(
        r"(?:what\s+do\s+i\s+need(?:\s+to\s+(?:get|buy|pick\s+up))?|"
        r"what\s+(?:should\s+i\s+)?(?:get|buy|pick\s+up)|"
        r"(?:need|have)\s+to\s+(?:get|buy|pick\s+up))"
        r".{0,40}?(?:at|from)\s+([A-Za-z][\w\s]{1,25}?)(?:\s*\?|$)",
        re.IGNORECASE,
    )
    _LIST_RE_PRE = re.compile(
        r"what(?:'s|\s+(?:else\s+)?(?:is|are))\s+(?:(?:on|in)\s+)?(?:my\s+)?([A-Za-z][\w\s]{1,25}?)\s+list",
        re.IGNORECASE,
    )
    # "show me / give me / tell me [the items on] the X list"
    _LIST_SHOW_RE = re.compile(
        r"\b(?:show(?:\s+me)?|give\s+me|tell\s+me|get|fetch)\b.{0,50}?"
        r"(?:the\s+|my\s+)([A-Za-z][\w\s]{1,25}?)\s+list\s*\??$",
        re.IGNORECASE,
    )
    _sm_pre = _STORE_RE_PRE.search(user_message) or _LIST_RE_PRE.search(user_message) or _LIST_SHOW_RE.search(user_message)
    if _sm_pre:
        _list_name = _sm_pre.group(1).strip().rstrip("?").strip()
        _list_name = re.sub(r"^the\s+", "", _list_name, flags=re.IGNORECASE).strip()
        _list_name = re.sub(r"\s+(?:store|shop|supermarket|market|pharmacy)$", "", _list_name, flags=re.IGNORECASE).strip()
        logger.info(f"Pre-model list read intercept: list_name={_list_name!r}")
        # If an internal list exists by this name, always prefer it (consistent with delete).
        _internal_list_id = await db.get_list_id(user_id, re.sub(r"\s+lists?$", "", _list_name, flags=re.IGNORECASE).strip())
        if _internal_list_id is not None:
            todo_result = await handle_tool_call("todo_get_items", {"list_name": _list_name}, user_id)
            _history[user_id].append({"role": "assistant", "content": todo_result})
            return todo_result
        # No internal list — try AnyList
        if Config.ANYLIST_EMAIL:
            anylist_result = await handle_tool_call("anylist_get_list", {"list_name": _list_name}, user_id)
            if not anylist_result.startswith("Tool error:"):
                _history[user_id].append({"role": "assistant", "content": anylist_result})
                return anylist_result
            logger.info(f"AnyList has no list named {_list_name!r}, trying internal todo")
        # Fall back to internal todo
        todo_result = await handle_tool_call("todo_get_items", {"list_name": _list_name}, user_id)
        _history[user_id].append({"role": "assistant", "content": todo_result})
        return todo_result

    # Meal plan
    if Config.ANYLIST_EMAIL:
        _MEAL_WORDS_PRE = ("dinner", "lunch", "breakfast", "meal", "meals", "supper", "eating", "food")
        if any(w in user_message.lower() for w in _MEAL_WORDS_PRE):
            today_str = datetime.now(ZoneInfo(Config.TIMEZONE)).strftime("%Y-%m-%d")
            start_str = grounded_dates[0] if grounded_dates else today_str
            end_str = grounded_dates[-1] if grounded_dates else today_str
            logger.info(f"Pre-model meal plan intercept: {start_str} to {end_str}")
            fallback_result = await handle_tool_call(
                "anylist_get_meal_plan", {"start": start_str, "end": end_str}, user_id
            )
            quip = await _personality_quip(fallback_result, user_id=user_id)
            reply = fallback_result + ("\n\n" + quip if quip else "")
            _history[user_id].append({"role": "assistant", "content": reply})
            return reply

    # Web search — explicit "search for / look up / find out about X"
    _SEARCH_CMD_RE = re.compile(
        r"\b(?:search(?:\s+for)?|look\s+up|find(?:\s+(?:info(?:rmation)?(?:\s+on)?|out\s+about))?|google)\s+(?:for\s+)?(?:information\s+on\s+)?(.{3,})",
        re.IGNORECASE,
    )
    _sm_search = _SEARCH_CMD_RE.search(user_message)
    if _sm_search:
        _search_query = _sm_search.group(1).strip().rstrip("?").strip()
        logger.info(f"Pre-model web search intercept: query={_search_query!r}")
        try:
            from services import search as search_service
            data = await search_service.search(_search_query)
            results = data.get("results", [])
            if results:
                lines = []
                for r in results[:3]:
                    lines.append(f"• {r['title']}\n  {r['url']}\n  {r['snippet'][:200]}")
                reply = "\n\n".join(lines)
            else:
                reply = f"No results found for '{_search_query}'."
        except Exception as e:
            reply = f"Search failed: {e}"
        _history[user_id].append({"role": "assistant", "content": reply})
        return reply

    messages = [{"role": "system", "content": _system_prompt(memories, personality_name)}] + _history[user_id]

    MAX_ITERATIONS = 8
    _reminder_fallback_tried = False
    for iteration in range(MAX_ITERATIONS):
        try:
            logger.info(f"Sending to Ollama (iteration {iteration}, {len(messages)} messages, {len(all_tools)} tools)")
            response = await client_chat(all_tools, messages)
        except Exception as e:
            logger.error(f"Ollama API error: {e}", exc_info=True)
            return "Sorry, I couldn't reach the AI model. Please check that Ollama is running."

        try:
            content, tool_calls = _extract_message(response)
        except Exception as e:
            logger.error(f"Failed to parse Ollama response: {e}", exc_info=True)
            return "Sorry, I had trouble processing the response. Please try again."

        # Enforce dates from the grounded user message
        if tool_calls:
            _enforce_grounded_dates(tool_calls, grounded_dates)

        # No tool calls → check if the model embedded them as text
        if not tool_calls:
            logger.info(f"No tool calls at iteration {iteration}; model content: {content[:200]!r}")
            text_calls = _parse_text_tool_calls(content)
            if text_calls:
                _enforce_grounded_dates(text_calls, grounded_dates)
                logger.info(f"Found {len(text_calls)} text-embedded tool call(s); re-entering loop")
                clean = _strip_text_tool_calls(content)
                messages.append({
                    "role": "assistant",
                    "content": clean,
                    "tool_calls": [
                        {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                        for tc in text_calls
                    ],
                })
                for tc in text_calls:
                    fn_name = tc["name"]
                    fn_args = tc["arguments"]
                    logger.info(f"Text tool call: {fn_name}({fn_args}) for user {user_id}")
                    result = await handle_tool_call(fn_name, fn_args, user_id)
                    logger.info(f"Text tool result: {result[:200]}")
                    if fn_name == "memory_save":
                        _memory_saved = True
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": fn_name,
                        "content": result,
                    })
                continue

            # Fallback: if user asked to remember something but model didn't call memory_save, do it now
            if not _memory_saved:
                saved = await _maybe_save_memory(user_id, user_message)
                if saved:
                    logger.info(f"Memory fallback saved for user {user_id}: {saved}")
                    content = "Got it."

            stripped = content.strip()

            # Detect when the model described a tool call as text instead of executing one
            _fake_tool_call = bool(re.search(
                r"calling\s+tool|calling_tool|<tool_call>|executing\s+tool|\bfunction\s+call\b",
                stripped, re.IGNORECASE,
            ))
            if _fake_tool_call:
                logger.info(f"Model wrote a fake tool call as text: {stripped[:120]!r}")
                stripped = ""  # treat as empty so fallbacks can fire

            # Fallback: meal plan query — fires on empty response OR capability denial
            _DENIAL_RE = re.compile(
                r"do\s+not\s+have\s+access|don[''`]t\s+have\s+(?:access|information)|"
                r"cannot\s+(?:access|provide|tell|give|check|look\s+up)|"
                r"can[''`]t\s+(?:access|provide|tell|give|check|look\s+up)|"
                r"no\s+access\s+to|not\s+able\s+to\s+(?:access|provide)|"
                r"I\s+am\s+not\s+(?:able|capable)|"
                r"don[''`]t\s+have\s+that\s+information",
                re.IGNORECASE,
            )
            _CLARIFICATION_RE = re.compile(
                r"could\s+you\s+(?:please\s+)?(?:provide|clarify|specify|tell|give|share)|"
                r"can\s+you\s+(?:please\s+)?(?:provide|clarify|specify|tell|give|share)|"
                r"please\s+(?:provide|clarify|specify|share)|"
                r"what\s+(?:specific\s+)?dates?|"
                r"(?:start|end)\s+date|"
                r"what\s+would\s+you\s+like\s+to\s+do",
                re.IGNORECASE,
            )
            _model_denied = bool(_DENIAL_RE.search(stripped))
            _model_confused = bool(_CLARIFICATION_RE.search(stripped))
            # Bare HA fragment with no args: "ha.", "ha_turn_off", "haturnoff", etc.
            _HA_FRAGMENT_RE = re.compile(r'^ha[_.]?\w*$', re.IGNORECASE)
            _model_ha_fragment = bool(_HA_FRAGMENT_RE.match(stripped) and "entity_id" not in stripped)
            # Model narrated an HA action instead of calling the tool
            _HA_NARRATIVE_RE = re.compile(
                r'(?:shutting|turning|switching)\s+(?:off|on|down|up)|'
                r'(?:lights?|device)\s+(?:are\s+)?(?:now\s+)?(?:off|on)',
                re.IGNORECASE,
            )
            _model_ha_narrative = (
                bool(_HA_NARRATIVE_RE.search(stripped))
                and bool(_HA_ACTION_RE.search(user_message))
                and "entity_id" not in stripped
            )
            # Weather location confusion
            _WEATHER_CONFUSION_RE = re.compile(
                r'not\s+specified\s+a\s+location|no\s+location|location\s+(?:not\s+)?(?:specified|provided)|'
                r'which\s+(?:city|location|place)|what\s+(?:city|location|place)',
                re.IGNORECASE,
            )
            _model_weather_confused = (
                bool(_WEATHER_CONFUSION_RE.search(stripped))
                and any(w in user_message.lower() for w in ("weather", "temperature", "outside", "forecast"))
            )
            if _model_denied or _model_confused or _model_ha_fragment or _model_ha_narrative or _model_weather_confused:
                reason = ("denied" if _model_denied else
                          "HA fragment" if _model_ha_fragment else
                          "HA narrative" if _model_ha_narrative else
                          "weather confused" if _model_weather_confused else
                          "asked clarification")
                logger.info(f"Model {reason}: {stripped[:120]!r}")
                stripped = ""  # treat as empty so fallbacks fire

            # Fallback: reminder create — fires unconditionally at iteration 0 if time is parseable
            _REMINDER_WORDS = ("remind", "reminder", "alarm", "alert", "notify", "notification")
            if not _reminder_fallback_tried and iteration == 0:
                if any(w in user_message.lower() for w in _REMINDER_WORDS):
                    _reminder_fallback_tried = True
                    now_local = datetime.now(ZoneInfo(Config.TIMEZONE))
                    parsed_msg, parsed_fire_at = _parse_reminder_request(user_message, now_local)
                    if parsed_msg and parsed_fire_at:
                        fire_at_str = parsed_fire_at.isoformat()
                        logger.info(f"Reminder fallback: parsed msg={parsed_msg!r} fire_at={fire_at_str}")
                        fallback_result = await handle_tool_call(
                            "reminder_create", {"message": parsed_msg, "fire_at": fire_at_str}, user_id
                        )
                        quip = await _personality_quip(fallback_result, "this reminder that was just set", user_id)
                        reply = quip if quip else fallback_result
                        _history[user_id].append({"role": "assistant", "content": reply})
                        return reply
                    else:
                        logger.info("Reminder fallback: could not parse time/message from request")

            # Reject raw JSON blobs that aren't tool calls (already handled above)
            if not stripped:
                if _memory_saved:
                    content = "Noted."
                else:
                    content = "That one eluded me entirely. Try rephrasing — I'm usually better than this."
            elif stripped.startswith(("[", "{")):
                try:
                    data = json.loads(stripped)
                    # If it looks like a tool call list, _parse_text_tool_calls already ran — suppress
                    is_tool_list = (
                        isinstance(data, list) and data and isinstance(data[0], dict) and
                        any(k in data[0] for k in ("tool_name", "function", "name", "tool"))
                    )
                    if not is_tool_list:
                        logger.warning(f"Model returned raw JSON, suppressing: {stripped[:80]}")
                        content = "That one eluded me entirely. Try rephrasing — I'm usually better than this."
                except json.JSONDecodeError:
                    pass
            _history[user_id].append({"role": "assistant", "content": content})
            return content

        # Append the assistant's tool-call message to conversation
        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for tc in tool_calls
            ],
        })

        # Execute each tool call and feed results back
        for tc in tool_calls:
            fn_name = tc["name"]
            fn_args = tc["arguments"]
            tool_call_id = tc.get("id", fn_name)

            # Resolve the name the same way handle_tool_call will, so corrections apply
            # even when the model used a namespaced/aliased name.
            _resolved_name = fn_name
            if "." in _resolved_name:
                _resolved_name = _resolved_name.rsplit(".", 1)[-1]
            if _resolved_name in _TOOL_ALIASES:
                _resolved_name = _TOOL_ALIASES[_resolved_name]

            # For reminder_create: if user said a relative time ("in X minutes"),
            # override whatever the model computed with the Python-parsed value.
            if _resolved_name == "reminder_create" and re.search(r"\bin\s+\d+\s+\w+", user_message, re.IGNORECASE):
                now_local = datetime.now(ZoneInfo(Config.TIMEZONE))
                _, correct_fire_at = _parse_reminder_request(user_message, now_local)
                if correct_fire_at:
                    old = fn_args.get("fire_at", "")
                    fn_args["fire_at"] = correct_fire_at.isoformat()
                    if old != fn_args["fire_at"]:
                        logger.info(f"Corrected reminder fire_at: {old!r} -> {fn_args['fire_at']!r}")

            logger.info(f"Tool call: {fn_name}({fn_args}) for user {user_id}")
            result = await handle_tool_call(fn_name, fn_args, user_id)
            logger.info(f"Tool result: {result[:200]}")
            if _resolved_name == "memory_save":
                _memory_saved = True

            # Return certain results directly to prevent model reformatting / greeting preamble
            # Use _resolved_name so aliases (e.g. get_list → anylist_get_list) are caught too.
            if _resolved_name == "todo_get_items" and len(tool_calls) == 1:
                _history[user_id].append({"role": "assistant", "content": result})
                return result

            if _resolved_name == "reminder_list" and len(tool_calls) == 1:
                _history[user_id].append({"role": "assistant", "content": result})
                return result

            if _resolved_name == "get_calendar_events" and len(tool_calls) == 1:
                reply = "**\U0001f4c5 Calendar events:**\n\n" + result
                _history[user_id].append({"role": "assistant", "content": reply})
                return reply

            if _resolved_name == "anylist_get_list" and len(tool_calls) == 1 and fn_args.get("list_name"):
                _history[user_id].append({"role": "assistant", "content": result})
                return result

            if _resolved_name == "anylist_get_meal_plan" and len(tool_calls) == 1:
                quip = await _personality_quip(result, user_id=user_id)
                reply = result + ("\n\n" + quip if quip else "")
                _history[user_id].append({"role": "assistant", "content": reply})
                return reply

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": fn_name,
                "content": result,
            })

    # Fallback if we hit max iterations
    fallback = "That took more effort than it should have, and I still came up short. Try rephrasing."
    _history[user_id].append({"role": "assistant", "content": fallback})
    return fallback


async def client_chat(all_tools, messages):
    client = ollama.AsyncClient(host=Config.OLLAMA_HOST)
    return await client.chat(
        model=Config.OLLAMA_MODEL,
        messages=messages,
        tools=all_tools,
        options={"think": False},
    )
