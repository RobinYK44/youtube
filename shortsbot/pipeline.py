"""Glue: pick streamers -> find the best unused clip -> render -> upload."""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db, editor, youtube
from .config import DATA_DIR, config
from .twitch import Clip, Twitch

log = logging.getLogger("shortsbot")
twitch = Twitch(config.twitch_client_id, config.twitch_client_secret)
OUTPUT_DIR = DATA_DIR / "shorts"


def _csv_setting(key: str) -> set[str]:
    return {s for s in db.get_setting(key).split(",") if s}


def add_streamer(login: str) -> None:
    login = login.strip().lower()
    db.set_setting("streamers_add", ",".join(_csv_setting("streamers_add") | {login}))
    db.set_setting("streamers_remove", ",".join(_csv_setting("streamers_remove") - {login}))


def remove_streamer(login: str) -> None:
    login = login.strip().lower()
    db.set_setting("streamers_add", ",".join(_csv_setting("streamers_add") - {login}))
    db.set_setting("streamers_remove", ",".join(_csv_setting("streamers_remove") | {login}))


def streamer_list() -> list[str]:
    """Fixed list + streamers added via Discord + today's biggest live streamers."""
    removed = _csv_setting("streamers_remove")
    logins = list(dict.fromkeys(config.streamers + sorted(_csv_setting("streamers_add"))))
    if config.auto_discover_top > 0:
        try:
            for s in twitch.top_live_streamers(config.auto_discover_top, config.discover_language):
                if s["login"] not in logins:
                    logins.append(s["login"])
        except Exception:
            log.exception("Kon top live streamers niet ophalen")
    return [login for login in logins if login not in removed]


def find_candidates() -> list[Clip]:
    logins = streamer_list()
    users = twitch.user_ids(logins)
    candidates = []
    for login, (user_id, name) in users.items():
        try:
            clips = twitch.top_clips(login, user_id, name, config.clip_lookback_days)
        except Exception:
            log.exception("Clips ophalen mislukt voor %s", login)
            continue
        candidates += [
            c
            for c in clips
            if c.view_count >= config.min_clip_views
            and c.duration >= config.min_clip_seconds
            and c.duration <= config.max_short_seconds + 1
            and not db.is_known(c.id)
        ]
    try:
        games = twitch.game_names(sorted({c.game_id for c in candidates}))
    except Exception:
        log.exception("Gamenamen ophalen mislukt")
        games = {}
    for clip in candidates:
        clip.game = games.get(clip.game_id, "")
    candidates.sort(key=lambda c: c.view_count, reverse=True)
    return candidates


def pick_candidates(count: int, per_streamer: int = 3) -> list[Clip]:
    """Most-viewed unused clips, at most `per_streamer` of each streamer for variety."""
    picked, per = [], {}
    for clip in find_candidates():
        if per.get(clip.broadcaster_login, 0) < per_streamer:
            picked.append(clip)
            per[clip.broadcaster_login] = per.get(clip.broadcaster_login, 0) + 1
        if len(picked) == count:
            break
    return picked


def candidates_per_day() -> int:
    value = db.get_setting("candidates_per_day")
    return int(value) if value else config.candidates_per_day


def clip_from_row(row) -> Clip:
    return Clip(
        id=row["id"],
        url=row["url"] or f"https://clips.twitch.tv/{row['id']}",
        title=row["title"],
        broadcaster_login=row["broadcaster"],
        broadcaster_name=row["broadcaster_name"] or row["broadcaster"],
        view_count=row["views"],
        duration=0,
        created_at="",
        game=row["game"] or "",
    )


def video_path(clip_id: str) -> Path:
    return OUTPUT_DIR / f"{clip_id}.mp4"


def pick_clip() -> Clip | None:
    """Best clip by views, but avoid posting the same streamer twice in a row."""
    candidates = find_candidates()
    if not candidates:
        return None
    recent = [row["broadcaster"] for row in db.recent_uploads(2)]
    for clip in candidates:
        if clip.broadcaster_login not in recent:
            return clip
    return candidates[0]


def open_slots(now: datetime | None = None, hours: int = 24) -> list[datetime]:
    """Publish times in the coming `hours` that have no Short uploaded for them yet."""
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(config.timezone)
    local_now = now.astimezone(tz)
    taken = db.taken_slots()
    slots = []
    for day in range(hours // 24 + 1):
        date = (local_now + timedelta(days=day)).date()
        for value in config.publish_times:
            hour, minute = (int(part) for part in value.split(":"))
            slot = datetime(date.year, date.month, date.day, hour, minute, tzinfo=tz).astimezone(timezone.utc)
            if now < slot <= now + timedelta(hours=hours) and slot.isoformat() not in taken:
                slots.append(slot)
    return sorted(slots)


def render(clip: Clip) -> Path:
    return editor.make_short(clip, OUTPUT_DIR)


def preview(video: Path) -> Path | None:
    return editor.make_preview(video)


def publish(clip: Clip, video: Path, slot: datetime | None = None) -> str:
    """Upload now; with a slot, YouTube publishes it at that time (immediately if it is less than 15 min away)."""
    publish_at = slot if slot and slot > datetime.now(timezone.utc) + timedelta(minutes=15) else None
    try:
        video_id = youtube.upload(video, clip, publish_at)
    except Exception:
        db.mark(clip, "failed")
        raise
    db.mark(clip, "uploaded", video_id, slot.isoformat() if slot else "")
    video.unlink(missing_ok=True)
    return video_id
