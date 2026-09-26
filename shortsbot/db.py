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
        status TEXT,          -- candidate / uploaded / tiktok / rejected / failed / expired
        youtube_id TEXT,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    """
)
_columns = [row["name"] for row in _conn.execute("PRAGMA table_info(clips)")]
for _name in ("publish_at", "url", "broadcaster_name", "game", "score", "parts", "tiktok_status", "tiktok_id"):  # added after the first release
    if _name not in _columns:
        _conn.execute(f"ALTER TABLE clips ADD COLUMN {_name} TEXT DEFAULT ''")
# Older versions had approval buttons that did not survive a restart.
_conn.execute("UPDATE clips SET status = 'expired' WHERE status = 'pending'")
_conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_known(clip_id: str) -> bool:
    return _conn.execute("SELECT 1 FROM clips WHERE id = ?", (clip_id,)).fetchone() is not None


def mark(clip, status: str, youtube_id: str = "", publish_at: str = "") -> None:
    _conn.execute(
        "INSERT OR REPLACE INTO clips"
        " (id, broadcaster, title, views, status, youtube_id, updated_at, publish_at, url, broadcaster_name, game, score,"
        " parts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            clip.id, clip.broadcaster_login, clip.title, clip.view_count, status, youtube_id, _now(),
            publish_at, clip.url, clip.broadcaster_name, getattr(clip, "game", ""), str(getattr(clip, "score", "")),
            ",".join(part.id for part in getattr(clip, "parts", [])),
        ),
    )
    _conn.commit()


def get(clip_id: str) -> sqlite3.Row | None:
    return _conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()


def candidates() -> list[sqlite3.Row]:
    """Rendered Shorts waiting for the owner to pick them, best first."""
    return _conn.execute(
        "SELECT * FROM clips WHERE status = 'candidate' ORDER BY CAST(score AS REAL) DESC, views DESC"
    ).fetchall()


def expire_candidates(before: str) -> list[str]:
    ids = [
        row["id"]
        for row in _conn.execute("SELECT id FROM clips WHERE status = 'candidate' AND updated_at < ?", (before,))
    ]
    _conn.executemany("UPDATE clips SET status = 'expired' WHERE id = ?", [(i,) for i in ids])
    _conn.commit()
    return ids


def taken_slots() -> set[str]:
    rows = _conn.execute("SELECT publish_at FROM clips WHERE status = 'uploaded' AND publish_at != ''").fetchall()
    return {row["publish_at"] for row in rows}


def scheduled_after(moment: str) -> list[sqlite3.Row]:
    return _conn.execute(
        "SELECT * FROM clips WHERE status = 'uploaded' AND publish_at > ? ORDER BY publish_at", (moment,)
    ).fetchall()


def cancel_scheduled_after(moment: str) -> list[sqlite3.Row]:
    """Free the publish times of Shorts that are scheduled but not online yet."""
    rows = scheduled_after(moment)
    _conn.executemany(
        "UPDATE clips SET status = 'cancelled', tiktok_status = '' WHERE id = ?", [(r["id"],) for r in rows]
    )
    _conn.commit()
    return rows


def set_tiktok(clip_id: str, status: str, tiktok_id: str = "") -> None:
    """status: queued / posted / private / draft / failed"""
    _conn.execute("UPDATE clips SET tiktok_status = ?, tiktok_id = ? WHERE id = ?", (status, tiktok_id, clip_id))
    _conn.commit()


def tiktok_due(moment: str) -> list[sqlite3.Row]:
    """Shorts waiting for TikTok whose publish time has come, oldest first."""
    return _conn.execute(
        "SELECT * FROM clips WHERE tiktok_status = 'queued' AND (publish_at = '' OR publish_at <= ?)"
        " ORDER BY publish_at",
        (moment,),
    ).fetchall()


def tiktok_queue_size() -> int:
    return _conn.execute("SELECT COUNT(*) FROM clips WHERE tiktok_status = 'queued'").fetchone()[0]


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
