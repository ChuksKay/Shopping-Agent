import os
import aiosqlite

DB_PATH = os.getenv("DB_PATH", "shopping_agent.db")

_CREATE_CHATS = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id     INTEGER PRIMARY KEY,
    mode        TEXT    NOT NULL DEFAULT 'delivery',
    postal_code TEXT    NOT NULL DEFAULT '',
    store       TEXT    NOT NULL DEFAULT '',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

# max_price / brand added in v2; ALTER TABLE migration handles existing DBs
_CREATE_ITEMS = """
CREATE TABLE IF NOT EXISTS items (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    text      TEXT    NOT NULL,
    qty       INTEGER NOT NULL DEFAULT 1,
    max_price REAL    DEFAULT NULL,
    brand     TEXT    DEFAULT NULL,
    FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
)
"""

_CREATE_JOBS = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id     TEXT PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'pending',
    result_url TEXT,
    error      TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
)
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_CREATE_CHATS)
        await db.execute(_CREATE_ITEMS)
        await db.execute(_CREATE_JOBS)

        # Migrate existing items table — add columns if missing
        for col, typedef in [("max_price", "REAL"), ("brand", "TEXT")]:
            try:
                await db.execute(f"ALTER TABLE items ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # column already exists

        await db.commit()


# ── Chat ──────────────────────────────────────────────────────────────────────

async def get_chat(chat_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM chats WHERE chat_id = ?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def upsert_chat(
    chat_id: int,
    mode: str = "delivery",
    postal_code: str = "",
    store: str = "",
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO chats (chat_id, mode, postal_code, store)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                mode        = excluded.mode,
                postal_code = excluded.postal_code,
                store       = excluded.store
            """,
            (chat_id, mode, postal_code, store),
        )
        await db.commit()


# ── Items ─────────────────────────────────────────────────────────────────────

async def get_items(chat_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM items WHERE chat_id = ? ORDER BY id", (chat_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def add_items(chat_id: int, items: list[dict]) -> None:
    """
    items: list of dicts with keys: name, qty, max_price (opt), brand (opt)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """
            INSERT INTO items (chat_id, text, qty, max_price, brand)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    chat_id,
                    item["name"],
                    item["qty"],
                    item.get("max_price"),
                    item.get("brand"),
                )
                for item in items
            ],
        )
        await db.commit()


async def clear_items(chat_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM items WHERE chat_id = ?", (chat_id,))
        await db.commit()


# ── Jobs ──────────────────────────────────────────────────────────────────────

async def create_job(job_id: str, chat_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO jobs (job_id, chat_id, status) VALUES (?, ?, 'pending')",
            (job_id, chat_id),
        )
        await db.commit()
    return {"job_id": job_id, "chat_id": chat_id, "status": "pending"}


async def get_job(job_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def update_job(
    job_id: str,
    status: str,
    result_url: str | None = None,
    error: str | None = None,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE jobs
            SET status = ?, result_url = ?, error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE job_id = ?
            """,
            (status, result_url, error, job_id),
        )
        await db.commit()


async def get_pending_jobs() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_at ASC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
