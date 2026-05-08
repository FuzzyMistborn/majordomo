import os

class Config:
    # Telegram
    TELEGRAM_TOKEN: str = os.environ["TELEGRAM_TOKEN"]
    ALLOWED_USER_IDS: list[int] = [
        int(uid.strip())
        for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
        if uid.strip()
    ]

    # Tavily
    TAVILY_API_KEY: str = os.environ["TAVILY_API_KEY"]

    # Ollama
    OLLAMA_HOST: str = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    OLLAMA_MODEL: str = os.environ.get("OLLAMA_MODEL", "gemma3:4b")

    # Home Assistant
    HA_URL: str = os.environ.get("HA_URL", "")
    HA_TOKEN: str = os.environ.get("HA_TOKEN", "")
    HA_CALENDARS: list[str] = [
        c.strip()
        for c in os.environ.get("HA_CALENDARS", "").split(",")
        if c.strip()
    ]

    HA_ALLOWED_DOMAINS: list[str] = [
        d.strip()
        for d in os.environ.get("HA_ALLOWED_DOMAINS", "light,switch,input_boolean,script,automation,climate,cover,fan,media_player").split(",")
        if d.strip()
    ]

    # Database
    DB_PATH: str = os.environ.get("DB_PATH", "/data/bot.db")

    # Timezone
    TIMEZONE: str = os.environ.get("TIMEZONE", "UTC")

    # Conversation history window
    HISTORY_WINDOW: int = int(os.environ.get("HISTORY_WINDOW", "20"))
