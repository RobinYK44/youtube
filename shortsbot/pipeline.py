"""Glue: pick streamers -> find the best unused clip -> render -> upload."""
import hashlib
import logging
import random
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


COMPILATION_TITLES = [
    "Funniest streamer moments this week 😂",
    "Top 3 funniest streamer moments 😂",
    "Streamers are NOT okay 💀",
    "Chat could not believe this 💀",
    "Try not to laugh: streamer edition 😂",
]


def is_funny(clip: Clip) -> bool:
    title = clip.title.lower()
    return "funny" in youtube.moods(clip.title) or any(word in title for word in HOT_WORDS)


def pick_compilation(count: int = 3) -> list[Clip] | None:
    """`count` short, funny clips of different streamers, worst first so the best one is #1 at the end."""
    found = [c for c in find_candidates() if c.duration <= 35]
    ordered = [c for c in found if is_funny(c)] + [c for c in found if not is_funny(c)]
    parts, streamers = [], set()
    for clip in ordered:
        if clip.broadcaster_login not in streamers:
            parts.append(clip)
            streamers.add(clip.broadcaster_login)
        if len(parts) == count:
            return sorted(parts, key=lambda c: c.score)
    return None


def compilation_clip(parts: list[Clip]) -> Clip:
    """One Clip that stands for the whole compilation (used for the database, Discord and YouTube)."""
    best = parts[-1]
    return Clip(
        id="comp-" + hashlib.sha1("|".join(p.id for p in parts).encode()).hexdigest()[:12],
        url=best.url,
        title=random.choice(COMPILATION_TITLES),
        broadcaster_login=best.broadcaster_login,
        broadcaster_name=", ".join(p.broadcaster_name for p in reversed(parts)),
        view_count=sum(p.view_count for p in parts),
        duration=sum(min(p.duration, config.compilation_part_seconds) for p in parts),
        created_at=max(p.created_at for p in parts),
        score=round(sum(p.score for p in parts) / len(parts), 1),
        parts=parts,
    )


def publish_times() -> list[str]:
    """Times (HH:MM, local) at which Shorts go online. Can be changed from Discord with /tijden."""
    value = db.get_setting("publish_times")
    return value.split(",") if value else config.publish_times


def parse_times(text: str) -> list[str] | None:
    """'18:00, 21:00,0:00' -> ['00:00', '18:00', '21:00']; None when something is not a valid time."""
    times = []
    for part in text.replace(" ", ",").split(","):
        if not part:
            continue
        hour, sep, minute = part.partition(":")
        if not (hour.isdigit() and (not sep or minute.isdigit())):
            return None
        h, m = int(hour), int(minute or 0)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        times.append(f"{h:02d}:{m:02d}")
    return sorted(set(times)) or None


def candidates_per_day() -> int:
    value = db.get_setting("candidates_per_day")
    return int(value) if value else config.candidates_per_day


def clip_from_row(row) -> Clip:
    parts = [clip_from_row(r) for r in (db.get(i) for i in (row["parts"] or "").split(",") if i) if r]
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
        parts=parts,
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
        for value in publish_times():
            hour, minute = (int(part) for part in value.split(":"))
            slot = datetime(date.year, date.month, date.day, hour, minute, tzinfo=tz).astimezone(timezone.utc)
            if now < slot <= now + timedelta(hours=hours) and slot.isoformat() not in taken:
                slots.append(slot)
    return sorted(slots)


def render(clip: Clip) -> Path:
    if clip.parts:
        return editor.make_compilation(clip, OUTPUT_DIR)
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
