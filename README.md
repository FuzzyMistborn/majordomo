# Ollama Telegram Bot

A local-only AI assistant Telegram bot powered by Ollama (Gemma / any tool-capable model).

## Features

- 📋 **To-do lists** — create multiple named lists, add/update/check-off/delete items
- ⏰ **Reminders** — one-shot and recurring, persisted across restarts
- 📝 **Notes** — create, search by keyword, update, delete
- 🔍 **Web search** — via Kagi Search API, summarised by the AI
- 🏠 **Home Assistant** — query entity states, toggle/turn on/off devices
- 🔒 **User whitelist** — only allowed Telegram user IDs can interact

All data is stored locally in a SQLite database. Nothing leaves your network except Telegram API calls and Kagi search queries.

---

## Requirements

- Docker + Docker Compose
- [Ollama](https://ollama.ai) running on your host with a tool-capable model pulled
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- A [Kagi API key](https://kagi.com/settings?p=api)

---

## Setup

### 1. Pull a model in Ollama

```bash
ollama pull gemma3:4b
```

> **Important:** The model must support tool/function calling. Confirmed working:
> `gemma3:4b`, `llama3.1`, `llama3.2`, `qwen2.5`, `mistral-nemo`

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your values
```

Key values to set:
- `TELEGRAM_TOKEN` — from @BotFather
- `ALLOWED_USER_IDS` — your Telegram user ID (get it from [@userinfobot](https://t.me/userinfobot))
- `KAGI_API_KEY` — from your Kagi account settings
- `TIMEZONE` — IANA timezone string (e.g. `America/New_York`)
- `HA_URL` + `HA_TOKEN` — optional, for Home Assistant control

### 3. Build and run

```bash
docker compose up -d
docker compose logs -f
```

---

## Usage Examples

Just send natural language messages:

| What you say | What happens |
|---|---|
| `"Remind me to take meds at 8am every day"` | Creates a recurring daily reminder |
| `"Add eggs to my shopping list"` | Adds to the 'shopping' list (creates it if needed) |
| `"Create a note called Server Setup with the steps I just sent"` | Creates a note |
| `"Search for the latest Fedora Kinoite release"` | Searches Kagi, returns summary + links |
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

To generate a token:
1. Go to your HA profile → Security → Long-lived access tokens
2. Create a token and add it to `HA_TOKEN` in your `.env`

Control is limited to the domains listed in `HA_ALLOWED_DOMAINS`. The default is:
```
light,switch,input_boolean,script,automation
```

---

## Configuration Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | — | Telegram bot token |
| `ALLOWED_USER_IDS` | ✅ | — | Comma-separated Telegram user IDs |
| `KAGI_API_KEY` | ✅ | — | Kagi Search API key |
| `OLLAMA_HOST` | — | `http://host.docker.internal:11434` | Ollama URL |
| `OLLAMA_MODEL` | — | `gemma3:4b` | Model name |
| `HA_URL` | — | _(disabled)_ | Home Assistant URL |
| `HA_TOKEN` | — | _(disabled)_ | HA long-lived token |
| `HA_ALLOWED_DOMAINS` | — | `light,switch,...` | Allowed HA domains |
| `TIMEZONE` | — | `UTC` | IANA timezone for reminders |
| `HISTORY_WINDOW` | — | `20` | Conversation messages to retain |

---

## Data

The SQLite database is stored in a Docker volume at `/data/bot.db`. To back it up:

```bash
docker run --rm -v ollama-telegram-bot_bot_data:/data -v $(pwd):/backup \
  alpine cp /data/bot.db /backup/bot-backup.db
```

---

## Troubleshooting

**Bot doesn't respond to tool calls / acts confused**
- Ensure your Ollama model supports tool calling. Not all models do.
- Try `llama3.1:8b` or `qwen2.5:7b` if `gemma3:4b` is unreliable.

**"Could not reach the AI model"**
- Verify Ollama is running: `ollama list`
- Check `OLLAMA_HOST` — from inside Docker, use `http://host.docker.internal:11434`

**Reminders not firing**
- Check the container timezone matches `TIMEZONE` env var
- View logs: `docker compose logs -f`

**Can't control HA entities**
- Confirm the entity's domain is in `HA_ALLOWED_DOMAINS`
- Test the HA token: `curl -H "Authorization: Bearer TOKEN" http://HA_URL/api/`
