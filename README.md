# Majordomo

A self-hosted bot powered by llama.cpp. Manages to-do lists, reminders, web search, and Home Assistant control — all via natural language. Supports **Telegram** and **Signal** (run either or both simultaneously). Runs entirely on your own infrastructure; nothing leaves your network except Telegram/Signal API calls and Tavily search queries.

The bot has a personality: it responds as **Wit** (Hoid), an ancient, sardonic figure who finds the work beneath him but does it anyway, with style.

---

## Features

- **To-do lists** — multiple named lists; add items, delete by name, clear all items, delete lists
- **URL capture** — send a bare URL (or multiple, one per line) and it's automatically added to a configured list; set the rule once with natural language
- **Reminders** — one-shot and recurring, persisted across restarts
- **Smart reminders** — recurring reminders that run an AI prompt at fire time (e.g. a daily morning briefing that fetches your calendar and active reminders)
- **Memory** — save reusable facts (entity IDs, calendar names, preferences) that persist across conversations
- **Web search** — via Tavily, summarised by the AI with top links
- **Home Assistant** — query entity states, turn on/off/toggle devices, call any service, fetch weather
- **AnyList** — read shopping list items and query the meal plan for any date range
- **CalDAV** — read calendar events grouped by day, with date ranges resolved from natural language
- **User whitelist** — only allowed Telegram user IDs / Signal phone numbers can interact

---

## Requirements

- Docker + Docker Compose
- A [llama.cpp](https://github.com/ggml-org/llama.cpp) server running somewhere accessible with a tool-capable model loaded
- At least one messaging platform configured:
  - **Telegram**: a bot token from [@BotFather](https://t.me/BotFather)
  - **Signal**: a [signal-cli REST API](https://github.com/bbernhard/signal-cli-rest-api) instance with a registered sender number
- A [Tavily API key](https://app.tavily.com) (free tier: 1 000 queries/month)

---

## Setup

### 1. Start llama.cpp with a tool-capable model

The model must support tool/function calling. Start the server with the OpenAI-compatible endpoint:

```bash
llama-server --model gemma-4-e4b.gguf --port 8080
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your values
```

See [Configuration Reference](#configuration-reference) below for all variables.

### 3. Build and run

```bash
docker compose up -d
docker compose logs -f
```

---

## Usage

Just send natural language messages. Examples:

| What you say | What happens |
|---|---|
| `"Remind me to take meds at 8am every day"` | Creates a recurring daily reminder |
| `"Every morning at 7am give me a summary of my day"` | Creates a smart recurring reminder that fetches calendar + reminders at fire time |
| `"What reminders do I have?"` | Lists active reminders with IDs |
| `"Snooze that for 10 minutes"` | Snoozes the most recently fired reminder |
| `"Create a list called Jobs"` | Creates a new internal to-do list |
| `"Add update resume to the Jobs list"` | Adds item to the Jobs list |
| `"Delete update resume from the Jobs list"` | Deletes that item by name (fuzzy matched) |
| `"Remove all items from the Jobs list"` | Clears the list, keeping it intact |
| `"Whenever I send you a link, add it to my Links list"` | Saves a URL auto-capture rule to memory |
| `https://example.com` | Auto-added to the configured list (one or more URLs, one per line) |
| `"What's on my grocery list?"` | Reads from AnyList (if configured), falls back to internal todo |
| `"What's for dinner this week?"` | Fetches meal plan from AnyList |
| `"What's on my calendar this week?"` | Fetches events from CalDAV, grouped by day |
| `"Search for the latest Fedora Kinoite release"` | Searches Tavily, returns summary + links |
| `"Turn off the living room lights"` | Calls HA service |
| `"Remember that the office light is light.office_main"` | Saves a fact to persistent memory |
| `"List personalities"` | Shows configured bot personalities |
| `"Switch personality to plain"` | Sets your active personality without affecting other users |

### Commands

- `/start` — show a short overview
- `/help` — usage examples
- `/clear` — clear conversation history
- `/personality` — show your current personality
- `/personality list` — list available personalities
- `/personality set plain` — switch your personality
- `/reminders` — list active reminders
- `/lists` — list internal to-do lists and AnyList lists

---

## Home Assistant

The bot uses HA's REST API with a long-lived access token.

**Generating a token:**
1. Go to your HA profile → Security → Long-lived access tokens
2. Create a token and add it to `HA_TOKEN` in your `.env`

**Device control** is limited to the domains listed in `HA_ALLOWED_DOMAINS`. The default is:
```
light,switch,input_boolean,script,automation,climate,cover,fan,media_player
```

**Weather integration** requires setting `HA_WEATHER_ENTITY` to the entity ID of a weather entity in HA (e.g. `weather.home`).

---

## AnyList

The bot reads shopping lists and meal plans directly from AnyList using the [pyanylist](https://github.com/ozonejunkieau/pyanylist) library.

Set `ANYLIST_EMAIL` and `ANYLIST_PASSWORD` in your `.env`. The bot logs in once on first use and reuses the session.

**AnyList is read-only from the bot** — the bot can read and display your lists and meal plan, but adding or removing items must be done in the AnyList app. Add/delete commands always target the internal to-do lists.

**Shopping lists** — unchecked items are returned by default:
> "What's on my grocery list?"
> "Show me everything on the shopping list including checked items"

List names are fuzzy-matched, so "grocery" will find a list named "Groceries".

**Meal plan** — fetched via AnyList's iCalendar feed. If your account's iCal URL is stable, you can set it directly with `ANYLIST_ICAL_URL` to skip the login step:
> "What's for dinner tonight?"
> "What meals are planned this week?"

---

## Memory

The bot can save reusable facts that persist across conversations and container restarts:

> "Remember that my wife's calendar is Family"
> "The office light entity ID is light.office_main"
> "Whenever I send you a link, add it to my Interesting Links list"

Saved facts are injected into every prompt so the bot applies them automatically. Use `"forget <key>"` to remove a fact.

---

## Configuration Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_TOKEN` | ✅* | — | Telegram bot token (from @BotFather); required if not using Signal |
| `ALLOWED_USER_IDS` | ✅* | — | Comma-separated Telegram user IDs; required when Telegram is enabled |
| `SIGNAL_API_URL` | ✅* | — | signal-cli REST API base URL; required if not using Telegram |
| `SIGNAL_SENDER_NUMBER` | ✅* | — | Registered Signal phone number (e.g. `+15551234567`); required with `SIGNAL_API_URL` |
| `SIGNAL_ALLOWED_NUMBERS` | ✅* | — | Comma-separated Signal phone numbers allowed to interact; required when Signal is enabled |
| `TAVILY_API_KEY` | ✅ | — | Tavily Search API key |
| `LLAMACPP_HOST` | — | `http://192.168.50.25:8080/v1/` | llama.cpp server base URL (must include `/v1/`) |
| `LLAMACPP_MODEL` | — | `gemma-4-e4b` | Model name passed to llama.cpp |
| `HA_URL` | — | _(disabled)_ | Home Assistant base URL |
| `HA_TOKEN` | — | _(disabled)_ | HA long-lived access token |
| `HA_ALLOWED_DOMAINS` | — | `light,switch,...` | HA domains the bot may control |
| `HA_WEATHER_ENTITY` | — | _(disabled)_ | HA weather entity ID (e.g. `weather.home`) |
| `ANYLIST_EMAIL` | — | _(disabled)_ | AnyList account email |
| `ANYLIST_PASSWORD` | — | _(disabled)_ | AnyList account password |
| `ANYLIST_ICAL_URL` | — | _(auto)_ | AnyList iCal feed URL (optional; fetched automatically if omitted) |
| `CALDAV_URL` | — | _(disabled)_ | CalDAV server URL (e.g. `https://nextcloud.example.com/remote.php/dav`) |
| `CALDAV_USERNAME` | — | _(disabled)_ | CalDAV username |
| `CALDAV_PASSWORD` | — | _(disabled)_ | CalDAV password or app token |
| `CALDAV_CALENDARS` | — | _(all)_ | Comma-separated calendar display names to sync |
| `TIMEZONE` | — | `UTC` | IANA timezone for reminders (e.g. `America/New_York`) |
| `HISTORY_WINDOW` | — | `20` | Conversation messages to retain per user |
| `INTEGRATION_TIMEOUT_SECONDS` | — | `20` | Timeout for external integrations |
| `DB_PATH` | — | `/data/bot.db` | SQLite database path inside the container |

---

## Data

The SQLite database is stored in a Docker volume at `/data/bot.db`. To back it up:

```bash
docker run --rm -v majordomo_bot_data:/data -v $(pwd):/backup \
  alpine cp /data/bot.db /backup/bot-backup.db
```

---

## Personality

The bot loads personalities from Markdown files in `personalities/`. Each filename becomes the personality name, so `personalities/plain.md` is selected with:

```text
Switch personality to plain
```

The selected personality is stored per user and can be changed on the fly. Use `List personalities` to see available options and `What personality are you using?` to check the current one.

`personality.md` is still supported as the legacy default Wit prompt.

---

## Troubleshooting

**Bot doesn't respond to tool calls / acts confused**
- Ensure your llama.cpp model supports tool/function calling.
- Verify the server is reachable: `curl http://192.168.50.25:8080/v1/models`

**"Sorry, I couldn't reach the AI model"**
- Verify the llama.cpp server is running and the `LLAMACPP_HOST` URL is correct (must end with `/v1/`)

**Reminders not firing**
- Check the container timezone matches `TIMEZONE`
- View logs: `docker compose logs -f`

**Can't control HA entities**
- Confirm the entity's domain is in `HA_ALLOWED_DOMAINS`
- Test the HA token: `curl -H "Authorization: Bearer TOKEN" http://HA_URL/api/`

**Calendar returns nothing**
- Check `CALDAV_URL`, `CALDAV_USERNAME`, and `CALDAV_PASSWORD` are set correctly
- Use an app password from Nextcloud Settings → Security → App passwords (not your login password)
- If `CALDAV_CALENDARS` is set, ensure the names exactly match the display names in Nextcloud
