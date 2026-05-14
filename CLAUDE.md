# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the bot

```bash
# Build and run (primary workflow)
docker compose up -d
docker compose logs -f

# Rebuild after code changes
docker compose up -d --build
```

There is no test suite. The bot is validated by running it and sending messages through Telegram.

## Architecture

Majordomo is a Telegram bot that routes natural language to a llama.cpp-backed AI agent. The main flow:

```
Telegram update → main.py → agent.chat() → tool-calling loop → tool handlers → services
```

**`main.py`** — Telegram bot setup, command handlers (`/start`, `/help`, `/clear`), and the message handler that calls `agent.chat()` then strips markdown from the reply before sending.

**`ai/agent.py`** — Core request processing. Two-layer design:
1. **Pre-model intercepts** — regex/deterministic handlers that bypass the model entirely for common patterns (reminder list, reminder delete, snooze, HA on/off/toggle, weather, todo CRUD, meal plan, web search). These fire first in `chat()` to prevent hallucination.
2. **Tool-calling loop** — up to 8 iterations; feeds tool results back as messages. Includes fallback text-embedded tool call parsing (`_parse_text_tool_calls`) for models that write tool calls as plain text instead of using the native mechanism.

**`ai/tools.py`** — All 25+ tool definitions (OpenAI function-calling schema) plus `handle_tool_call()`, a large match/case dispatcher. Also contains `_TOOL_ALIASES` (100+ model-invented names → canonical names), argument normalization for HA/calendar/reminder params, and `get_active_tool_definitions()` which returns only tools for configured services.

**`database.py`** — async SQLite via `aiosqlite`. Tables: `todo_lists`, `todo_items`, `reminders`, `notes`, `memories`. All queries scope by `user_id`. `init_db()` runs on startup and handles migrations inline.

**`scheduler.py`** — APScheduler (AsyncIO) wrapping reminder firing. Supports one-shot (`DateTrigger`) and recurring (`CronTrigger` from JSON cron spec). "Smart" reminders re-invoke `agent.chat()` at fire time instead of sending a static message. `_last_fired` tracks the most recently fired reminder per user for snooze.

**`config.py`** — All configuration from environment variables with defaults. Optional services (HA, CalDAV, AnyList) are disabled when their env vars are absent.

**`services/`** — Thin async wrappers:
- `homeassistant.py` — HA REST API (httpx)
- `calendar.py` — CalDAV via the `caldav` library
- `anylist.py` — Shopping lists and meal plan via `pyanylist`; meal plan falls back to `ANYLIST_ICAL_URL` if set
- `search.py` — Tavily web search

**`personality.md`** — System prompt personality (Wit/Hoid from Cosmere). Loaded lazily on first use; injected into `_system_prompt()` before the operational rules block.

## Key design patterns

**Pre-model intercepts vs. tool loop**: Most deterministic operations (CRUD on todos, reminders, weather) are handled entirely in `agent.py` before touching the model. This prevents hallucination for routine commands. Only genuinely ambiguous NL requests go to the model.

**Date grounding**: `_inject_date_context()` rewrites relative date keywords ("tomorrow", "this week") to `keyword (YYYY-MM-DD)` in the user message before it goes to the model. `_enforce_grounded_dates()` then validates that tool call date args match those grounded values.

**Tool alias resolution**: `handle_tool_call()` resolves namespaced names (`reminder_api.create_reminder`), method-dispatch patterns (`method='delete_event'` in args), and aliases from `_TOOL_ALIASES` before the match/case block.

**Smart reminders**: When `smart=True`, `scheduler.py` calls `agent.chat()` at fire time with the stored instruction, so the reminder dynamically assembles e.g. a morning briefing from live calendar/reminder data.

**Per-user conversation history**: `_history` dict in `agent.py` (in-memory, lost on restart). Trimmed to `min(HISTORY_WINDOW, 6)` messages to limit context size. `/clear` wipes it via `clear_history()`.

**AnyList vs internal todo routing**: Shopping list reads try AnyList first (if configured); writes always go to the internal SQLite todo lists since AnyList is read-only from the bot.
