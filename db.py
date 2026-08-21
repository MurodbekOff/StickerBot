"""Tiny SQLite layer:
- which sticker sets belong to which user (for /mypacks, /addsticker picker)
- per-user setting: whether downloaded videos get a credit caption
- co-editing: a share-link token per pack, and who has been granted add
  access to a pack through that link
"""
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone

DB_PATH = "fstik.db"


def init_db(path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS packs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                caption_enabled INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pack_editors (
                pack_name TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY (pack_name, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pack_share_tokens (
                pack_name TEXT PRIMARY KEY,
                token TEXT NOT NULL UNIQUE
            )
            """
        )
        conn.commit()


def add_pack(user_id: int, name: str, title: str, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO packs (user_id, name, title, created_at) VALUES (?, ?, ?, ?)",
            (user_id, name, title, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_user_packs(user_id: int, path: str = DB_PATH) -> list[tuple[str, str]]:
    """Returns list of (name, title) for a user's own packs, newest first.

    Only returns packs this user_id *owns* -- packs they can co-edit via a
    share link don't show up here, since editing there happens entirely
    through the /start deep link instead of this picker.
    """
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute(
            "SELECT name, title FROM packs WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        return cur.fetchall()


def get_pack_owner(name: str, path: str = DB_PATH) -> int | None:
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute("SELECT user_id FROM packs WHERE name = ?", (name,))
        row = cur.fetchone()
        return row[0] if row else None


def get_pack_title(name: str, path: str = DB_PATH) -> str | None:
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute("SELECT title FROM packs WHERE name = ?", (name,))
        row = cur.fetchone()
        return row[0] if row else None


def set_pack_title(name: str, title: str, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE packs SET title = ? WHERE name = ?", (title, name))
        conn.commit()


def get_caption_enabled(user_id: int, path: str = DB_PATH) -> bool:
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute("SELECT caption_enabled FROM settings WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return bool(row[0]) if row else True  # default: caption ON


def set_caption_enabled(user_id: int, enabled: bool, path: str = DB_PATH) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            INSERT INTO settings (user_id, caption_enabled) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET caption_enabled = excluded.caption_enabled
            """,
            (user_id, int(enabled)),
        )
        conn.commit()


# ---------- co-editing ----------

def add_editor(pack_name: str, user_id: int, path: str = DB_PATH) -> None:
    """Records that user_id has been granted add-only access to pack_name
    (they opened a valid co-edit link). Idempotent."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO pack_editors (pack_name, user_id, added_at) VALUES (?, ?, ?)",
            (pack_name, user_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_editor_ids(pack_name: str, path: str = DB_PATH) -> list[int]:
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute(
            "SELECT user_id FROM pack_editors WHERE pack_name = ? ORDER BY added_at",
            (pack_name,),
        )
        return [row[0] for row in cur.fetchall()]


def get_or_create_share_token(pack_name: str, path: str = DB_PATH) -> str:
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute("SELECT token FROM pack_share_tokens WHERE pack_name = ?", (pack_name,))
        row = cur.fetchone()
        if row:
            return row[0]
        token = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO pack_share_tokens (pack_name, token) VALUES (?, ?)",
            (pack_name, token),
        )
        conn.commit()
        return token


def reset_share_token(pack_name: str, path: str = DB_PATH) -> str:
    """Generates a fresh token for the pack, invalidating the old link.
    Does NOT remove already-granted editors -- it only stops the *old* link
    from granting new access."""
    token = uuid.uuid4().hex
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            INSERT INTO pack_share_tokens (pack_name, token) VALUES (?, ?)
            ON CONFLICT(pack_name) DO UPDATE SET token = excluded.token
            """,
            (pack_name, token),
        )
        conn.commit()
    return token


def get_pack_by_token(token: str, path: str = DB_PATH) -> str | None:
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute("SELECT pack_name FROM pack_share_tokens WHERE token = ?", (token,))
        row = cur.fetchone()
        return row[0] if row else None