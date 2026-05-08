# Majordomo

A self-hosted Telegram bot powered by Ollama. Manages to-do lists, reminders, notes, web search, and Home Assistant control — all via natural language. Runs entirely on your own infrastructure; nothing leaves your network except Telegram API calls and Tavily search queries.

The bot has a personality: it responds as **Wit** (Hoid), an ancient, sardonic figure who finds the work beneath him but does it anyway, with style.

---

## Features

- **To-do lists** — multiple named lists; add, update, check off, delete items
- **Reminders** — one-shot and recurring, persisted across restarts
- **Smart reminders** — recurring reminders that run an AI prompt at fire time (e.g. a daily morning briefing that fetches your calendar and active reminders)
- **Notes** — create with tags, search by keyword, update, delete
- **Web search** — via Tavily, summarised by the AI with top links
- **Home Assistant** — query entity states, turn on/off/toggle devices, call any service, read calendar events
- **User whitelist** — only allowed Telegram user IDs can interact

---

## Requirements

- Docker + Docker Compose
- [Ollama](https://ollama.ai) running somewhere accessible with a tool-capable model pulled
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- A [Tavily API key](https://app.tavily.com) (free tier: 1 000 queries/month)

---

## Setup

### 1. Pull a model in Ollama

```bash
ollama pull gemma3:4b
```

The model must support tool/function calling. Confirmed working: `gemma3:4b`, `llama3.1`, `llama3.2`, `qwen2.5`, `mistral-nemo`.

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
| `"Add eggs to my shopping list"` | Adds to the 'shopping' list (creates it if needed) |
| `"What's on my calendar this week?"` | Fetches events from your HA calendars |
| `"Create a note called Server Setup with the steps I just sent"` | Creates a tagged note |
| `"Search for the latest Fedora Kinoite release"` | Searches Tavily, returns summary + links |
| `"Turn off the living room lights"` | Calls HA service |
| `"What reminders do I have?"` | Lists active reminders |
| `"Mark the eggs item as done"` | Checks off the item |

### Commands

- `/start` — introduction
- `/help` — usage examples
- `/clear` — clear conversation history (bot forgets context)

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

**Calendar integration** requires setting `HA_CALENDARS` to a comma-separated list of calendar entity IDs from your HA instance (e.g. calendar entities from Nextcloud, Google Calendar, etc.):
```
HA_CALENDARS=calendar.personal,calendar.family
```

---

## Configuration Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | — | Telegram bot token (from @BotFather) |
| `ALLOWED_USER_IDS` | ✅ | — | Comma-separated Telegram user IDs |
| `TAVILY_API_KEY` | ✅ | — | Tavily Search API key |
| `OLLAMA_HOST` | — | `http://host.docker.internal:11434` | Ollama instance URL |
| `OLLAMA_MODEL` | — | `gemma3:4b` | Model name (must support tool calling) |
| `HA_URL` | — | _(disabled)_ | Home Assistant base URL |
| `HA_TOKEN` | — | _(disabled)_ | HA long-lived access token |
| `HA_ALLOWED_DOMAINS` | — | `light,switch,...` | HA domains the bot may control |
| `HA_CALENDARS` | — | _(disabled)_ | Comma-separated HA calendar entity IDs |
| `TIMEZONE` | — | `UTC` | IANA timezone for reminders (e.g. `America/New_York`) |
| `HISTORY_WINDOW` | — | `20` | Conversation messages to retain per user |
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

The bot's voice is defined in `personality.md`. By default it responds as Wit (Hoid from the Cosmere) — dry, sardonic, brief. You can replace `personality.md` with any system prompt to change the character entirely.

---

## Troubleshooting

**Bot doesn't respond to tool calls / acts confused**
- Ensure your Ollama model supports tool calling. Not all models do.
- Try `llama3.1:8b` or `qwen2.5:7b` if `gemma3:4b` is unreliable.

**"Sorry, I couldn't reach the AI model"**
- Verify Ollama is running: `ollama list`
- Check `OLLAMA_HOST` — from inside Docker, use `http://host.docker.internal:11434`

**Reminders not firing**
- Check the container timezone matches `TIMEZONE`
- View logs: `docker compose logs -f`

**Can't control HA entities**
- Confirm the entity's domain is in `HA_ALLOWED_DOMAINS`
- Test the HA token: `curl -H "Authorization: Bearer TOKEN" http://HA_URL/api/`

**Calendar returns nothing**
- Set `HA_CALENDARS` to the exact calendar entity IDs from your HA instance
- Confirm `HA_URL` and `HA_TOKEN` are set correctly
