"""Glue: pick streamers -> find the best unused clip -> render -> upload."""
import logging
import statistics
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


# Words people put in clip titles when something funny, crazy or weird happens.
HOT_WORDS = [
    "wtf", "what", "chat", "no way", "bro", "insane", "crazy", "omg", "bruh", "actually", "real",
    "?!", "??", "!!", "💀", "😭", "😂", "🤣", "😱", "🔥",
]


def age_hours(clip: Clip, now: datetime | None = None) -> float:
    try:
        created = datetime.fromisoformat(clip.created_at.replace("Z", "+00:00"))
    except ValueError:
        return 24.0
    return max(1.0, ((now or datetime.now(timezone.utc)) - created).total_seconds() / 3600)


def viral_score(clip: Clip, streamer_median: float, now: datetime | None = None) -> float:
    """Guess how likely a clip is to do well as a Short. The bot cannot watch the video, so it uses:
    how fast the views come in, whether the clip stands out for this streamer, title words and length."""
    score = clip.view_count / age_hours(clip, now) ** 0.5
    standout = min(3.0, max(0.5, clip.view_count / max(streamer_median, 1)))
    score *= standout**0.5
    title = clip.title.lower()
    if youtube.moods(clip.title) or any(word in title for word in HOT_WORDS):
        score *= 1.3
    # Short Shorts do best: 15-35 s is ideal, long clips get cut and may lose context.
    if 15 <= clip.duration <= 35:
        score *= 1.2
    elif clip.duration < 12:
        score *= 0.85
    elif clip.duration > 45:
        score *= 0.9
    return round(score, 1)


def find_candidates() -> list[Clip]:
    logins = streamer_list()
    users = twitch.user_ids(logins)
    candidates = []
    now = datetime.now(timezone.utc)
    for login, (user_id, name) in users.items():
        try:
            clips = twitch.top_clips(login, user_id, name, config.clip_lookback_days)
        except Exception:
            log.exception("Clips ophalen mislukt voor %s", login)
            continue
        median = statistics.median([c.view_count for c in clips]) if clips else 1
        for clip in clips:
            clip.score = viral_score(clip, median, now)
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
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def pick_candidates(count: int, per_streamer: int = 3) -> list[Clip]:
    """Highest-scoring unused clips, at most `per_streamer` of each streamer for variety."""
    found = find_candidates()
    picked, per = [], {}
    for clip in found:
        if per.get(clip.broadcaster_login, 0) < per_streamer:
            picked.append(clip)
            per[clip.broadcaster_login] = per.get(clip.broadcaster_login, 0) + 1
        if len(picked) == count:
            return picked
    # Not enough variety left: fill up with the next best clips, whoever the streamer is.
    picked += [clip for clip in found if clip not in picked][: count - len(picked)]
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
        score=float(row["score"] or 0),
    )


def video_path(clip_id: str) -> Path:
    return OUTPUT_DIR / f"{clip_id}.mp4"


def pick_clip() -> Clip | None:
    """Highest-scoring clip, but avoid posting the same streamer twice in a row."""
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
