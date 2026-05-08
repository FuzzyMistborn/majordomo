"""
AI agent: manages per-user conversation history and drives the Ollama tool-calling loop.
"""

import json
import os
import logging
import re
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import ollama

from ai.tools import TOOL_DEFINITIONS, handle_tool_call
from config import Config

logger = logging.getLogger(__name__)

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


def _system_prompt() -> str:
    now = datetime.now(ZoneInfo(Config.TIMEZONE)).strftime("%A, %B %d %Y %H:%M %Z")
    personality = _load_personality()
    ops = f"""Today is {now} ({Config.TIMEZONE}).

You have tools for: to-do lists, reminders, notes, web search, and Home Assistant control.

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
- To get calendar events: ha_get_calendar_events(start="{datetime.now(ZoneInfo(Config.TIMEZONE)).strftime('%Y-%m-%d')}", end="..."). Use ISO 8601 dates.
- When presenting calendar results, output them EXACTLY as returned by the tool — do NOT reformat, bold, or add Markdown to times or event names.
- Once an HA action succeeds, confirm briefly and stop. Do not call more tools.
"""
    if personality:
        return personality + "\n\n---\n\n" + ops
    return "You are a helpful personal assistant.\n\n" + ops


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


def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks from model output."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.strip()


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
    _history[user_id].append({"role": "user", "content": user_message})
    _trim_history(user_id)

    all_tools = TOOL_DEFINITIONS
    messages = [{"role": "system", "content": _system_prompt()}] + _history[user_id]

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

        # No tool calls → final text response
        if not tool_calls:
            if not content.strip():
                content = "I processed your request but had nothing to say. Please try again."
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

            # For list results, return directly to prevent model reformatting
            if fn_name == "todo_get_items" and len(tool_calls) == 1:
                _history[user_id].append({"role": "assistant", "content": result})
                return result

            if fn_name == "ha_get_calendar_events" and len(tool_calls) == 1:
                header = "\U0001f4c5 *Calendar events:*\n\n"
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
