"""Small SQLite store: which clips were used, and runtime settings changed from Discord."""
import sqlite3
from datetime import datetime, timezone

from .config import DATA_DIR

_conn = sqlite3.connect(DATA_DIR / "shortsbot.db", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS clips (
        id TEXT PRIMARY KEY,
        broadcaster TEXT,
        title TEXT,
        views INTEGER,
        status TEXT,          -- uploaded / pending / rejected / failed / expired
        youtube_id TEXT,
        updated_at TEXT,
        publish_at TEXT       -- UTC time YouTube publishes the video; empty = immediately
    );
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    """
)
if "publish_at" not in [row["name"] for row in _conn.execute("PRAGMA table_info(clips)")]:
    _conn.execute("ALTER TABLE clips ADD COLUMN publish_at TEXT DEFAULT ''")
# Approval buttons do not survive a restart, so their slots must be freed again.
_conn.execute("UPDATE clips SET status = 'expired' WHERE status = 'pending'")
_conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_known(clip_id: str) -> bool:
    return _conn.execute("SELECT 1 FROM clips WHERE id = ?", (clip_id,)).fetchone() is not None


def mark(clip, status: str, youtube_id: str = "", publish_at: str = "") -> None:
    _conn.execute(
        "INSERT OR REPLACE INTO clips (id, broadcaster, title, views, status, youtube_id, updated_at, publish_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (clip.id, clip.broadcaster_login, clip.title, clip.view_count, status, youtube_id, _now(), publish_at),
    )
    _conn.commit()


def taken_slots() -> set[str]:
    rows = _conn.execute(
        "SELECT publish_at FROM clips WHERE status IN ('uploaded', 'pending') AND publish_at != ''"
    ).fetchall()
    return {row["publish_at"] for row in rows}


def scheduled_after(moment: str) -> list[sqlite3.Row]:
    return _conn.execute(
        "SELECT * FROM clips WHERE status = 'uploaded' AND publish_at > ? ORDER BY publish_at", (moment,)
    ).fetchall()


def uploads_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    row = _conn.execute(
        "SELECT COUNT(*) FROM clips WHERE status = 'uploaded' AND updated_at >= ?", (today,)
    ).fetchone()
    return row[0]


def recent_uploads(limit: int = 5) -> list[sqlite3.Row]:
    return _conn.execute(
        "SELECT * FROM clips WHERE status = 'uploaded' ORDER BY updated_at DESC LIMIT ?", (limit,)
    ).fetchall()


def get_setting(key: str, default: str = "") -> str:
    row = _conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    _conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
    _conn.commit()
