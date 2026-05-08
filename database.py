import aiosqlite
from config import Config

DB = Config.DB_PATH

CREATE_TABLES = """
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
"""

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript(CREATE_TABLES)
        # Migrate: add smart column if it doesn't exist yet
        try:
            await db.execute("ALTER TABLE reminders ADD COLUMN smart INTEGER NOT NULL DEFAULT 0")
            await db.commit()
        except Exception:
            pass  # Column already exists
        await db.commit()


# ── Todo Lists ──────────────────────────────────────────────────────────────

async def create_todo_list(user_id: int, name: str) -> dict:
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
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "DELETE FROM todo_lists WHERE user_id = ? AND LOWER(name) = LOWER(?)", (user_id, list_name)
        )
        await db.commit()
        return cur.rowcount > 0


async def get_list_id(user_id: int, list_name: str) -> int | None:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT id FROM todo_lists WHERE user_id = ? AND LOWER(name) = LOWER(?)", (user_id, list_name)
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def add_todo_item(user_id: int, list_name: str, content: str) -> dict:
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


async def update_todo_item(item_id: int, content: str | None = None, done: bool | None = None) -> bool:
    fields, vals = [], []
    if content is not None:
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
        cur = await db.execute(
            f"UPDATE todo_items SET {', '.join(fields)} WHERE id = ?", vals
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_todo_item(item_id: int) -> bool:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("DELETE FROM todo_items WHERE id = ?", (item_id,))
        await db.commit()
        return cur.rowcount > 0


# ── Reminders ───────────────────────────────────────────────────────────────

async def create_reminder(user_id: int, message: str, fire_at: str, recurrence: str | None, recurrence_human: str | None, smart: bool = False) -> dict:
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


async def delete_reminder(user_id: int, reminder_id: int) -> bool:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id)
        )
        await db.commit()
        return cur.rowcount > 0


# ── Notes ───────────────────────────────────────────────────────────────────

async def create_note(user_id: int, title: str, content: str, tags: str = "") -> dict:
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
        fields.append("title = ?"); vals.append(title)
    if content is not None:
        fields.append("content = ?"); vals.append(content)
    if tags is not None:
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
