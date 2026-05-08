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
from ai.tools import TOOL_DEFINITIONS, _TOOL_ALIASES, handle_tool_call
from config import Config

logger = logging.getLogger(__name__)

# Built once: all tool names the parser should recognise in bare funcname(...) text
_KNOWN_TOOLS: frozenset[str] = frozenset(
    {td["function"]["name"] for td in TOOL_DEFINITIONS} | set(_TOOL_ALIASES.keys())
)

# Personality loaded lazily on first use
_personality = None


def _load_personality() -> str:
    global _personality
    if _personality is not None:
        return _personality
    for path in ["/app/personality.md", "personality.md"]:
        try:
            with open(path) as f:
                _personality = f.read().strip()
            logger.info(f"Loaded personality from {path} ({len(_personality)} chars)")
            return _personality
        except FileNotFoundError:
            continue
    logger.warning("personality.md not found — running without personality file")
    _personality = ""
    return _personality

# Per-user conversation history: user_id -> list of message dicts
_history: dict[int, list[dict]] = defaultdict(list)


def _system_prompt(memories: list[dict] | None = None) -> str:
    now = datetime.now(ZoneInfo(Config.TIMEZONE)).strftime("%A, %B %d %Y %H:%M %Z")
    personality = _load_personality()
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

You have tools for: to-do lists, reminders, notes, web search, Home Assistant control, and calendar management.

Operational rules:
- Be concise (Telegram chat). Do NOT use Markdown formatting (no **bold**, no *italic*, no `code`). Plain text only.
- For reminders confirm the exact time back to the user.
- For a daily briefing/summary, create a smart recurring reminder (smart=true) with an instruction like "Give me a summary of today's calendar events, current weather, and any reminders I have".
- For web searches give a 2-3 sentence summary and top 3 links.
- Never show raw JSON. Format results as readable text.

Home Assistant rules:
- Use ha_turn_on, ha_turn_off, or ha_toggle for simple on/off control — provide the exact entity_id.
- If the user gives a name but not an entity_id, make your best guess (e.g. "office light" -> entity_id="light.office").
- Use ha_get_state to check a single entity, ha_get_states with domains=["light"] to see all lights.
- Use ha_call_service for advanced control (brightness, temperature, etc).
- NEVER use search_web for Home Assistant. If entity not found, ask the user for the exact entity_id.
Calendar: use get_calendar_events(start, end) to list events. start and end are ISO 8601 dates (YYYY-MM-DD). If no date is specified, default to today. You can only READ calendar events — creating, updating, and deleting events is not supported.

Memory rules:
- When the user says "remember", "note that", or tells you a reusable fact (e.g. "my wife's calendar is Family", "the office light is light.office_main"), you MUST call the memory_save tool immediately. Do NOT just say you'll remember — call the tool.
- Always apply saved memories when resolving names — check the list below before guessing.
- When you apply a memory, do so silently. Don't announce it.
"""
    if memories:
        mem_lines = "\n".join(f"- {m['key']}: {m['value']}" for m in memories)
        ops += f"\nLearned facts about this user:\n{mem_lines}\n"

    if personality:
        return personality + "\n\n---\n\n" + ops
    return "You are a helpful personal assistant.\n\n" + ops


_DATE_KEYWORDS = (
    "today", "tonight", "tomorrow", "yesterday",
    "this morning", "this afternoon", "this evening", "this week",
    "next week", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
)

def _inject_date_context(message: str) -> str:
    """Replace relative date keywords with ISO dates so the model never has to compute them."""
    lower = message.lower()
    if not any(w in lower for w in _DATE_KEYWORDS):
        return message
    tz = ZoneInfo(Config.TIMEZONE)
    now = datetime.now(tz)

    # Direct substitutions — replace the word in-place so the model sees the ISO date
    direct = {
        "tonight":   now.strftime("%Y-%m-%d"),
        "today":     now.strftime("%Y-%m-%d"),
        "tomorrow":  (now + timedelta(days=1)).strftime("%Y-%m-%d"),
        "yesterday": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
    }
    result = message
    for keyword, date_str in direct.items():
        result = re.sub(r"\b" + re.escape(keyword) + r"\b", date_str, result, flags=re.IGNORECASE)

    # For weekday names keep a reference block (used by model for scheduling)
    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    weekday_dates = {}
    for i, name in enumerate(day_names):
        days_ahead = (i - now.weekday()) % 7 or 7
        weekday_dates[name] = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    # Replace weekday names in-place too
    for name, date_str in weekday_dates.items():
        result = re.sub(r"\b" + name + r"\b", date_str, result, flags=re.IGNORECASE)

    return result


_ISO_DATE_PART_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_DATE_FIELDS = {"start", "end", "new_start", "new_end", "fire_at",
                "start_date", "end_date", "new_start_date", "new_end_date",
                "start_time", "end_time"}  # catches full-datetime values passed in *_time fields


def _extract_date_parts(text: str) -> list[str]:
    """Return unique YYYY-MM-DD values found in text, in order."""
    seen: set[str] = set()
    result = []
    for m in _ISO_DATE_PART_RE.finditer(text):
        d = m.group(1)
        if d not in seen:
            seen.add(d)
            result.append(d)
    return result


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
    r'(?:remember|note|save|store)\s+that\s+["\']?(.+?)["\']?\s+'
    r'(?:is(?:\s+called)?|means?|refers?\s+to|=)\s+["\']?(.+?)["\']?\s*$',
    re.IGNORECASE,
)


async def _maybe_save_memory(user_id: int, user_message: str) -> str | None:
    """Fallback: parse a 'remember that X is Y' message and save directly."""
    m = _MEMORY_RE.search(user_message)
    if not m:
        return None
    key = m.group(1).strip().strip('"\'')
    value = m.group(2).strip().strip('"\'')
    if key and value:
        await db.save_memory(user_id, key, value)
        logger.info(f"Fallback memory save: {key!r} = {value!r}")
        return f"{key} = {value}"
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

    # Pattern 2: JSON array of tool calls — [{"tool_name": "...", "parameters": {...}}, ...]
    stripped = content.strip()
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

    return results


def _strip_text_tool_calls(content: str) -> str:
    return _TEXT_TOOL_RE.sub("", content).strip()


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


async def chat(user_id: int, user_message: str) -> str:
    """
    Process a user message through the Ollama agent loop.
    Returns the assistant's final reply as a string.
    """
    logger.info(f"chat() called for user {user_id}: {user_message[:80]!r}")
    grounded = _inject_date_context(user_message)
    grounded_dates = _extract_date_parts(grounded)
    _history[user_id].append({"role": "user", "content": grounded})
    _trim_history(user_id)
    _memory_saved = False

    memories = await db.get_memories(user_id)
    all_tools = TOOL_DEFINITIONS
    messages = [{"role": "system", "content": _system_prompt(memories)}] + _history[user_id]

    MAX_ITERATIONS = 8
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
            # Reject raw JSON blobs that aren't tool calls (already handled above)
            stripped = content.strip()
            if not stripped:
                content = "Done."
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
                        content = "Done."
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

            logger.info(f"Tool call: {fn_name}({fn_args}) for user {user_id}")
            result = await handle_tool_call(fn_name, fn_args, user_id)
            logger.info(f"Tool result: {result[:200]}")
            if fn_name == "memory_save":
                _memory_saved = True

            # Return certain results directly to prevent model reformatting / greeting preamble
            if fn_name == "todo_get_items" and len(tool_calls) == 1:
                _history[user_id].append({"role": "assistant", "content": result})
                return result

            if fn_name == "get_calendar_events" and len(tool_calls) == 1:
                header = "\U0001f4c5 Calendar events:\n\n"
                reply = header + result
                _history[user_id].append({"role": "assistant", "content": reply})
                return reply


            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": fn_name,
                "content": result,
            })

    # Fallback if we hit max iterations
    fallback = "I ran into a problem completing that request. Please try rephrasing."
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
