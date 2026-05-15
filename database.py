import aiosqlite
from config import Config

DB = Config.DB_PATH

_FIRE_AT_MAX_CHARS = 100  # ISO 8601 datetime strings are always well under this

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS todo_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS todo_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id INTEGER NOT NULL REFERENCES todo_lists(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    fire_at TIMESTAMP NOT NULL,
    recurrence TEXT,          -- NULL = one-shot; cron expression or natural spec stored as JSON
    recurrence_human TEXT,    -- human readable description
    fired INTEGER NOT NULL DEFAULT 0,
    smart INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT DEFAULT '',     -- comma separated
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, key)
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id, key)
);

CREATE TABLE IF NOT EXISTS signal_users (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_todo_lists_user_name ON todo_lists(user_id, name);
CREATE INDEX IF NOT EXISTS idx_todo_items_list_created ON todo_items(list_id, created_at);
CREATE INDEX IF NOT EXISTS idx_reminders_user_active ON reminders(user_id, fired, recurrence, fire_at);
CREATE INDEX IF NOT EXISTS idx_reminders_active_fire_at ON reminders(fired, recurrence, fire_at);
CREATE INDEX IF NOT EXISTS idx_memories_user_key ON memories(user_id, key);
CREATE INDEX IF NOT EXISTS idx_user_settings_user_key ON user_settings(user_id, key);
"""


def _bounded_text(value: object, field: str, max_chars: int, *, allow_empty: bool = False) -> str:
    text = "" if value is None else str(value).strip()
    if not text and not allow_empty:
        raise ValueError(f"{field} cannot be empty.")
    if len(text) > max_chars:
        raise ValueError(f"{field} is too long (max {max_chars} characters).")
    return text


async def _migration_applied(db: aiosqlite.Connection, version: str) -> bool:
    cur = await db.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,))
    return await cur.fetchone() is not None


async def _record_migration(db: aiosqlite.Connection, version: str) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)",
        (version,),
    )


async def _apply_migrations(db: aiosqlite.Connection) -> None:
    if not await _migration_applied(db, "001_add_reminder_smart"):
        cur = await db.execute("PRAGMA table_info(reminders)")
        columns = {row[1] for row in await cur.fetchall()}
        if "smart" not in columns:
            await db.execute("ALTER TABLE reminders ADD COLUMN smart INTEGER NOT NULL DEFAULT 0")
        await _record_migration(db, "001_add_reminder_smart")

    if not await _migration_applied(db, "002_signal_users_sequence"):
        # Seed the autoincrement sequence so Signal user IDs start at 10_000_000_000,
        # safely above any realistic Telegram user ID.
        await db.execute(
            "INSERT OR IGNORE INTO signal_users (id, phone) VALUES (9999999999, '__sentinel__')"
        )
        await db.execute("DELETE FROM signal_users WHERE phone = '__sentinel__'")
        await _record_migration(db, "002_signal_users_sequence")


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.executescript(CREATE_TABLES)
        await _apply_migrations(db)
        await db.commit()


# ── Todo Lists ──────────────────────────────────────────────────────────────

async def create_todo_list(user_id: int, name: str) -> dict:
    name = _bounded_text(name, "List name", Config.MAX_LIST_NAME_CHARS)
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        try:
            cur = await db.execute(
                "INSERT INTO todo_lists (user_id, name) VALUES (?, ?) RETURNING *",
                (user_id, name),
            )
            row = await cur.fetchone()
            await db.commit()
            return dict(row)
        except aiosqlite.IntegrityError:
            raise ValueError(f"A list named '{name}' already exists.")


async def get_todo_lists(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM todo_lists WHERE user_id = ? ORDER BY name", (user_id,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def delete_todo_list(user_id: int, list_name: str) -> bool:
    list_name = _bounded_text(list_name, "List name", Config.MAX_LIST_NAME_CHARS)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT id FROM todo_lists WHERE user_id = ? AND LOWER(name) = LOWER(?)",
            (user_id, list_name),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        list_id = row[0]
        await db.execute("DELETE FROM todo_items WHERE list_id = ?", (list_id,))
        cur = await db.execute("DELETE FROM todo_lists WHERE id = ?", (list_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_list_id(user_id: int, list_name: str) -> int | None:
    list_name = _bounded_text(list_name, "List name", Config.MAX_LIST_NAME_CHARS)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT id FROM todo_lists WHERE user_id = ? AND LOWER(name) = LOWER(?)", (user_id, list_name)
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def add_todo_item(user_id: int, list_name: str, content: str) -> dict:
    list_name = _bounded_text(list_name, "List name", Config.MAX_LIST_NAME_CHARS)
    content = _bounded_text(content, "To-do item", Config.MAX_TODO_ITEM_CHARS)
    list_id = await get_list_id(user_id, list_name)
    if list_id is None:
        raise ValueError(f"No list named '{list_name}' found.")
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "INSERT INTO todo_items (list_id, content) VALUES (?, ?) RETURNING *",
            (list_id, content),
        )
        row = await cur.fetchone()
        await db.commit()
        return dict(row)


async def get_todo_items(user_id: int, list_name: str) -> list[dict]:
    list_name = _bounded_text(list_name, "List name", Config.MAX_LIST_NAME_CHARS)
    list_id = await get_list_id(user_id, list_name)
    if list_id is None:
        raise ValueError(f"No list named '{list_name}' found.")
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM todo_items WHERE list_id = ? ORDER BY created_at", (list_id,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def update_todo_item(item_id: int, content: str | None = None, done: bool | None = None, user_id: int | None = None) -> bool:
    fields, vals = [], []
    if content is not None:
        content = _bounded_text(content, "To-do item", Config.MAX_TODO_ITEM_CHARS)
        fields.append("content = ?")
        vals.append(content)
    if done is not None:
        fields.append("done = ?")
        vals.append(1 if done else 0)
    if not fields:
        return False
    fields.append("updated_at = CURRENT_TIMESTAMP")
    vals.append(item_id)
    async with aiosqlite.connect(DB) as db:
        if user_id is None:
            cur = await db.execute(
                f"UPDATE todo_items SET {', '.join(fields)} WHERE id = ?", vals
            )
        else:
            vals.append(user_id)
            cur = await db.execute(
                f"""UPDATE todo_items SET {', '.join(fields)}
                    WHERE id = ?
                    AND list_id IN (
                        SELECT id FROM todo_lists WHERE user_id = ?
                    )""",
                vals,
            )
        await db.commit()
        return cur.rowcount > 0


async def delete_todo_item(item_id: int, user_id: int | None = None) -> bool:
    async with aiosqlite.connect(DB) as db:
        if user_id is None:
            cur = await db.execute("DELETE FROM todo_items WHERE id = ?", (item_id,))
        else:
            cur = await db.execute(
                """DELETE FROM todo_items
                   WHERE id = ?
                   AND list_id IN (
                       SELECT id FROM todo_lists WHERE user_id = ?
                   )""",
                (item_id, user_id),
            )
        await db.commit()
        return cur.rowcount > 0


async def clear_todo_list(user_id: int, list_name: str) -> int:
    """Delete all items from a list, keeping the list itself. Returns count removed."""
    list_name = _bounded_text(list_name, "List name", Config.MAX_LIST_NAME_CHARS)
    list_id = await get_list_id(user_id, list_name)
    if list_id is None:
        raise ValueError(f"No list named '{list_name}' found.")
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("DELETE FROM todo_items WHERE list_id = ?", (list_id,))
        await db.commit()
        return cur.rowcount


# ── Reminders ───────────────────────────────────────────────────────────────

async def create_reminder(user_id: int, message: str, fire_at: str, recurrence: str | None, recurrence_human: str | None, smart: bool = False) -> dict:
    message = _bounded_text(message, "Reminder message", Config.MAX_REMINDER_MESSAGE_CHARS)
    fire_at = _bounded_text(fire_at, "Reminder fire_at", _FIRE_AT_MAX_CHARS)
    recurrence = _bounded_text(recurrence, "Reminder recurrence", 1000, allow_empty=True) if recurrence is not None else None
    recurrence_human = _bounded_text(recurrence_human, "Reminder recurrence description", 200, allow_empty=True) if recurrence_human is not None else None
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """INSERT INTO reminders (user_id, message, fire_at, recurrence, recurrence_human, smart)
               VALUES (?, ?, ?, ?, ?, ?) RETURNING *""",
            (user_id, message, fire_at, recurrence, recurrence_human, 1 if smart else 0),
        )
        row = await cur.fetchone()
        await db.commit()
        return dict(row)


async def get_reminders(user_id: int, include_fired: bool = False) -> list[dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM reminders WHERE user_id = ?"
        params: list = [user_id]
        if not include_fired:
            query += " AND (fired = 0 OR recurrence IS NOT NULL)"
        query += " ORDER BY fire_at"
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_all_active_reminders() -> list[dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM reminders WHERE fired = 0 OR recurrence IS NOT NULL ORDER BY fire_at"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def mark_reminder_fired(reminder_id: int, next_fire_at: str | None = None) -> None:
    async with aiosqlite.connect(DB) as db:
        if next_fire_at:
            await db.execute(
                "UPDATE reminders SET fire_at = ? WHERE id = ?", (next_fire_at, reminder_id)
            )
        else:
            await db.execute(
                "UPDATE reminders SET fired = 1 WHERE id = ?", (reminder_id,)
            )
        await db.commit()


async def get_reminder_by_id(reminder_id: int, user_id: int) -> dict | None:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def snooze_reminder(reminder_id: int, new_fire_at: str) -> bool:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "UPDATE reminders SET fire_at = ?, fired = 0 WHERE id = ?", (new_fire_at, reminder_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_reminder(user_id: int, reminder_id: int) -> bool:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id)
        )
        await db.commit()
        return cur.rowcount > 0


# ── Notes ───────────────────────────────────────────────────────────────────

async def create_note(user_id: int, title: str, content: str, tags: str = "") -> dict:
    title = _bounded_text(title, "Note title", Config.MAX_NOTE_TITLE_CHARS)
    content = _bounded_text(content, "Note content", Config.MAX_NOTE_CONTENT_CHARS)
    tags = _bounded_text(tags, "Note tags", 500, allow_empty=True)
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "INSERT INTO notes (user_id, title, content, tags) VALUES (?, ?, ?, ?) RETURNING *",
            (user_id, title, content, tags),
        )
        row = await cur.fetchone()
        await db.commit()
        return dict(row)


async def search_notes(user_id: int, query: str) -> list[dict]:
    query = _bounded_text(query, "Note search query", Config.MAX_SEARCH_QUERY_CHARS)
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        pattern = f"%{query}%"
        cur = await db.execute(
            """SELECT * FROM notes
               WHERE user_id = ?
               AND (title LIKE ? OR content LIKE ? OR tags LIKE ?)
               ORDER BY updated_at DESC""",
            (user_id, pattern, pattern, pattern),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_note(note_id: int, user_id: int) -> dict | None:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_note(note_id: int, user_id: int, title: str | None = None, content: str | None = None, tags: str | None = None) -> bool:
    fields, vals = [], []
    if title is not None:
        title = _bounded_text(title, "Note title", Config.MAX_NOTE_TITLE_CHARS)
        fields.append("title = ?"); vals.append(title)
    if content is not None:
        content = _bounded_text(content, "Note content", Config.MAX_NOTE_CONTENT_CHARS)
        fields.append("content = ?"); vals.append(content)
    if tags is not None:
        tags = _bounded_text(tags, "Note tags", 500, allow_empty=True)
        fields.append("tags = ?"); vals.append(tags)
    if not fields:
        return False
    fields.append("updated_at = CURRENT_TIMESTAMP")
    vals += [note_id, user_id]
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            f"UPDATE notes SET {', '.join(fields)} WHERE id = ? AND user_id = ?", vals
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_note(note_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id)
        )
        await db.commit()
        return cur.rowcount > 0


# ── Memories ─────────────────────────────────────────────────────────────────

async def save_memory(user_id: int, key: str, value: str) -> None:
    key = _bounded_text(key, "Memory key", Config.MAX_MEMORY_KEY_CHARS)
    value = _bounded_text(value, "Memory value", Config.MAX_MEMORY_VALUE_CHARS)
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """INSERT INTO memories (user_id, key, value)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value, created_at = CURRENT_TIMESTAMP""",
            (user_id, key, value),
        )
        await db.commit()


async def get_memories(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM memories WHERE user_id = ? ORDER BY key", (user_id,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def delete_memory(user_id: int, key: str) -> bool:
    key = _bounded_text(key, "Memory key", Config.MAX_MEMORY_KEY_CHARS)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "DELETE FROM memories WHERE user_id = ? AND LOWER(key) = LOWER(?)", (user_id, key)
        )
        await db.commit()
        return cur.rowcount > 0


# ── User Settings ─────────────────────────────────────────────────────────────

async def save_user_setting(user_id: int, key: str, value: str) -> None:
    key = _bounded_text(key, "Setting key", Config.MAX_SETTING_KEY_CHARS)
    value = _bounded_text(value, "Setting value", Config.MAX_SETTING_VALUE_CHARS)
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """INSERT INTO user_settings (user_id, key, value)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = CURRENT_TIMESTAMP""",
            (user_id, key, value),
        )
        await db.commit()


async def get_user_setting(user_id: int, key: str) -> str | None:
    key = _bounded_text(key, "Setting key", Config.MAX_SETTING_KEY_CHARS)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
            (user_id, key),
        )
        row = await cur.fetchone()
        return row[0] if row else None


# ── Signal Users ──────────────────────────────────────────────────────────────

async def get_or_create_signal_user_id(phone: str) -> int:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT id FROM signal_users WHERE phone = ?", (phone,))
        row = await cur.fetchone()
        if row:
            return row[0]
        cur = await db.execute("INSERT INTO signal_users (phone) VALUES (?)", (phone,))
        await db.commit()
        return cur.lastrowid


async def get_signal_phone_by_user_id(user_id: int) -> str | None:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT phone FROM signal_users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None
