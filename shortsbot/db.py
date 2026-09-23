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
        status TEXT,          -- uploaded / rejected / failed
        youtube_id TEXT,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    """
)
_conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_known(clip_id: str) -> bool:
    return _conn.execute("SELECT 1 FROM clips WHERE id = ?", (clip_id,)).fetchone() is not None


def mark(clip, status: str, youtube_id: str = "") -> None:
    _conn.execute(
        "INSERT OR REPLACE INTO clips VALUES (?, ?, ?, ?, ?, ?, ?)",
        (clip.id, clip.broadcaster_login, clip.title, clip.view_count, status, youtube_id, _now()),
    )
    _conn.commit()


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
