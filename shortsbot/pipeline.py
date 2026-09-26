"""Glue: pick streamers -> find the best unused clip -> render -> upload."""
import hashlib
import logging
import math
import random
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db, editor, youtube, ytclips
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


# Clips of today's biggest live streamers rank below every favourite, also in the kiesmodus order.
DISCOVERED_WEIGHT = 0.25


# Always searched too, on top of STREAMERS in .env: big Twitch streamers with lots of funny, loud moments.
MORE_STREAMERS = ["marlon", "plaqueboymax", "jasontheween", "silky", "yourragegaming", "agent00", "dukedennis", "sketch"]
# The owner's favourites: their clips get a head start.
TOP_STREAMERS = {"jynxzi", "stableronaldo", "lacy", "marlon"}
TOP_BOOST = 1.5


def favourite_streamers() -> list[str]:
    """Fixed list + streamers added via Discord, minus the ones removed via Discord."""
    removed = _csv_setting("streamers_remove")
    logins = dict.fromkeys(config.streamers + MORE_STREAMERS + sorted(_csv_setting("streamers_add")))
    return [login for login in logins if login not in removed]


def streamer_list() -> list[str]:
    """Favourite streamers + today's biggest live streamers."""
    removed = _csv_setting("streamers_remove")
    logins = favourite_streamers()
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
    # A clip that gets far more views than this streamer's other clips is usually one where something happened.
    standout = min(4.0, max(0.5, clip.view_count / max(streamer_median, 1)))
    score *= standout**0.8
    title = clip.title.lower()
    if youtube.moods(clip.title) or any(word in title for word in HOT_WORDS):
        score *= 1.5
    # Short Shorts do best: 12-30 s is ideal, long clips get cut and may lose context.
    if 12 <= clip.duration <= 30:
        score *= 1.2
    elif clip.duration < 12:
        score *= 0.85
    elif clip.duration > 45:
        score *= 0.9
    return round(score, 1)


# ---- learning from our own views ---------------------------------------------------------------------------

STATS_DAYS = 30
STATS_MIN_AGE = timedelta(hours=48)  # a Short needs a couple of days before its views say something
STATS_MIN_SHORTS = 5  # too little to learn from below this
STATS_SHRINK = 3  # a streamer with only 1 or 2 Shorts stays close to average


def update_view_stats() -> int:
    """Read the current views of our Shorts from YouTube. Returns how many were updated."""
    rows = db.uploads_since_publish((datetime.now(timezone.utc) - timedelta(days=STATS_DAYS)).isoformat())
    if not rows:
        return 0
    views = youtube.video_views([row["youtube_id"] for row in rows])
    for row in rows:
        if row["youtube_id"] in views:
            db.set_views(row["id"], views[row["youtube_id"]])
    return len(views)


def _published(row) -> datetime:
    return datetime.fromisoformat(row["publish_at"] or row["updated_at"])


def measured_shorts() -> list:
    now = datetime.now(timezone.utc)
    rows = db.uploads_since_publish((now - timedelta(days=STATS_DAYS)).isoformat())
    return [r for r in rows if r["yt_views"] != "" and now - _published(r) >= STATS_MIN_AGE]


def performance() -> dict[str, float]:
    """Per streamer/channel: how much better (>1) or worse (<1) their Shorts do on our channel than average.
    Uses the log of the views, so one lucky viral Short does not decide everything."""
    rows = measured_shorts()
    if len(rows) < STATS_MIN_SHORTS:
        return {}
    logs: dict[str, list[float]] = {}
    for row in rows:
        logs.setdefault(row["broadcaster"], []).append(math.log1p(int(row["yt_views"])))
    overall = statistics.mean(v for values in logs.values() for v in values)
    return {
        login: round(min(2.5, max(0.5, math.exp((sum(values) + STATS_SHRINK * overall) / (len(values) + STATS_SHRINK) - overall))), 2)
        for login, values in logs.items()
    }


def find_candidates(days: int | None = None) -> list[Clip]:
    logins = streamer_list()
    favourites = set(favourite_streamers())
    learned = performance()
    users = twitch.user_ids(logins)
    candidates = []
    now = datetime.now(timezone.utc)
    for login, (user_id, name) in users.items():
        try:
            clips = twitch.top_clips(login, user_id, name, days or config.clip_lookback_days)
        except Exception:
            log.exception("Clips ophalen mislukt voor %s", login)
            continue
        median = statistics.median([c.view_count for c in clips]) if clips else 1
        for clip in clips:
            clip.score = viral_score(clip, median, now)
            clip.score = round(clip.score * learned.get(login, 1.0), 1)  # streamers that do well on our channel first
            if login in TOP_STREAMERS:
                clip.score = round(clip.score * TOP_BOOST, 1)
            elif login not in favourites:
                clip.score = round(clip.score * DISCOVERED_WEIGHT, 1)
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
        clip.title = youtube.better_title(clip)  # after scoring: the score uses the original title
    # Favourites always first; the live top streamers only fill up when the favourites run out.
    candidates.sort(key=lambda c: (c.broadcaster_login in favourites, c.score), reverse=True)
    return candidates


def twitch_candidates(needed: int) -> list[Clip]:
    """Fresh clips first. When the favourites have fewer than `needed` new ones, add popular clips from the
    last CLIP_LOOKBACK_MAX_DAYS days: a clip with lots of views two weeks ago can still do well as a Short."""
    found = find_candidates()
    favourites = set(favourite_streamers())
    if sum(c.broadcaster_login in favourites for c in found) >= needed:
        return found
    if config.clip_lookback_max_days <= config.clip_lookback_days:
        return found
    seen = {c.id for c in found}
    older = [c for c in find_candidates(config.clip_lookback_max_days) if c.id not in seen]
    fresh_favourites = [c for c in found if c.broadcaster_login in favourites]
    older_favourites = [c for c in older if c.broadcaster_login in favourites]
    rest = [c for c in found + older if c.broadcaster_login not in favourites]
    return fresh_favourites + older_favourites + rest


def pick_candidates(count: int, per_streamer: int = 3) -> list[Clip]:
    """Highest-scoring unused clips, at most `per_streamer` of each streamer for variety."""
    found = twitch_candidates(count)
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
    found = [c for c in twitch_candidates(count) if c.duration <= 35]
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


def youtube_channels() -> list[str]:
    """YouTube channels to clip from: .env list + added via Discord, minus removed via Discord."""
    removed = _csv_setting("youtube_remove")
    channels = dict.fromkeys(config.youtube_channels + sorted(_csv_setting("youtube_add")))
    return [c for c in channels if c not in removed]


def add_youtube_channel(name: str) -> None:
    name = name.strip().lstrip("@").lower()
    db.set_setting("youtube_add", ",".join(_csv_setting("youtube_add") | {name}))
    db.set_setting("youtube_remove", ",".join(_csv_setting("youtube_remove") - {name}))


def remove_youtube_channel(name: str) -> None:
    name = name.strip().lstrip("@").lower()
    db.set_setting("youtube_add", ",".join(_csv_setting("youtube_add") - {name}))
    db.set_setting("youtube_remove", ",".join(_csv_setting("youtube_remove") | {name}))


def moment_used(video_id: str, start: float) -> bool:
    """True when this moment, or one overlapping it, was already made into a Short."""
    for known in db.known_ids(f"yt-{video_id}-"):
        other = known.rsplit("-", 1)[1]
        if other.isdigit() and abs(int(other) - start) < ytclips.MOMENT_SECONDS:
            return True
    return False


def youtube_candidates(count: int, per_video: int = 2) -> list[Clip]:
    """The most re-watched unused moments from the newest videos and streams of the YouTube channels."""
    found = []
    learned = performance()
    for channel in youtube_channels():
        for url in ytclips.latest_videos(channel):
            try:
                info = ytclips.video_info(url)
            except Exception:
                log.exception("YouTube-video ophalen mislukt: %s", url)
                continue
            if ytclips.age_days(info) > config.clip_lookback_max_days:
                continue
            moments = [(s, h) for s, h in ytclips.moments(info, 4) if not moment_used(info["id"], s)]
            for start, heat in moments[:per_video]:
                clip = ytclips.make_clip(info, start, heat)
                clip.score = round(clip.score * learned.get(clip.broadcaster_login, 1.0), 1)
                found.append(clip)
    found = list({c.id: c for c in found}.values())  # a stream can show up on both the videos and streams tab
    found.sort(key=lambda c: c.score, reverse=True)
    return found[:count]


def clip_video(url: str, count: int, vyro: bool = False, hashtags: str = "") -> tuple[list[Clip], str]:
    """Moments from one YouTube video (/knip). Returns (clips, video title)."""
    info = ytclips.video_info(url)
    tags = []
    if vyro:
        handle = (info.get("uploader_id") or info.get("channel") or "").lstrip("@").lower()
        tags = [t.lstrip("#").lower() for t in hashtags.replace(",", " ").split()] or [handle, handle + "partner"]
    moments = [(s, h) for s, h in ytclips.moments(info, count + 10) if not moment_used(info["id"], s)][:count]
    clips = [ytclips.make_clip(info, s, h, "vyro" if vyro else "youtube", tags) for s, h in moments]
    return clips, info.get("title") or url


def pick_mixed(count: int) -> list[Clip]:
    """Kiesmodus: half Twitch clips, half YouTube moments (fewer YouTube when there are not enough)."""
    try:
        youtube_clips = youtube_candidates(count // 2) if youtube_channels() else []
    except Exception:
        log.exception("YouTube-clips zoeken mislukt")
        youtube_clips = []
    twitch_clips = pick_candidates(count - len(youtube_clips))
    mixed = []
    for i in range(max(len(youtube_clips), len(twitch_clips))):
        mixed += twitch_clips[i : i + 1] + youtube_clips[i : i + 1]
    return mixed


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
        source=row["source"] or "twitch",
        tags=[t for t in (row["tags"] or "").split(",") if t],
    )


def video_path(clip_id: str) -> Path:
    return OUTPUT_DIR / f"{clip_id}.mp4"


def pick_clip() -> Clip | None:
    """Alternate between a Twitch clip and a YouTube moment; use the other one when one has nothing."""
    last = db.recent_uploads(1)
    youtube_first = bool(last) and (last[0]["source"] or "twitch") == "twitch"
    order = (pick_youtube_clip, pick_twitch_clip) if youtube_first else (pick_twitch_clip, pick_youtube_clip)
    for pick in order:
        try:
            clip = pick()
        except Exception:
            log.exception("Clips zoeken mislukt in %s", pick.__name__)
            clip = None
        if clip:
            return clip
    return None


def pick_youtube_clip() -> Clip | None:
    found = youtube_candidates(1)
    return found[0] if found else None


def pick_twitch_clip() -> Clip | None:
    """Highest-scoring clip, but avoid posting the same streamer twice in a row."""
    candidates = twitch_candidates(1)
    if not candidates:
        return None
    recent = [row["broadcaster"] for row in db.recent_uploads(2)]
    favourites = set(favourite_streamers())
    group = [c for c in candidates if c.broadcaster_login in favourites] or candidates
    for clip in group:
        if clip.broadcaster_login not in recent:
            return clip
    return group[0]  # a favourite twice in a row beats a random live streamer


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
    if clip.source != "twitch":
        return editor.make_youtube_short(clip, OUTPUT_DIR)
    if clip.parts:
        return editor.make_compilation(clip, OUTPUT_DIR)
    return editor.make_short(clip, OUTPUT_DIR)


def preview(video: Path) -> Path | None:
    return editor.make_preview(video)


def tiktok_version(video: Path) -> Path | None:
    return editor.make_tiktok_version(video)


def publish(clip: Clip, video: Path, slot: datetime | None = None) -> str:
    """Upload now; with a slot, YouTube publishes it at that time (immediately if it is less than 15 min away)."""
    publish_at = slot if slot and slot > datetime.now(timezone.utc) + timedelta(minutes=15) else None
    try:
        video_id = youtube.upload(video, clip, publish_at)
    except Exception:
        db.mark(clip, "failed")
        raise
    db.mark(clip, "uploaded", video_id, slot.isoformat() if slot else "")
    if tiktok_enabled():
        db.set_tiktok(clip.id, "queued")  # posted at its publish time by the bot, see post_tiktok
    else:
        video.unlink(missing_ok=True)
    return video_id


def tiktok_enabled() -> bool:
    try:
        from . import tiktok
    except ImportError:  # an older update.bat did not download tiktok.py
        return False
    return tiktok.enabled() and db.get_setting("tiktok_paused") != "1"


def tiktok_needs_audit(exc: Exception) -> bool:
    """TikTok lets an app that is not approved yet only post to private accounts."""
    return "can_only_post_to_private_accounts" in str(exc)


def post_tiktok(row) -> tuple[str, str]:
    """Post one queued Short to TikTok. Returns (publish_id, status): posted / private / draft.
    While the TikTok app is not approved it goes to the TikTok app's inbox as a draft instead."""
    from . import tiktok

    clip = clip_from_row(row)
    video = video_path(clip.id)
    if not video.exists():
        db.set_tiktok(clip.id, "failed")
        raise RuntimeError("videobestand niet meer gevonden")
    try:
        publish_id, public = tiktok.upload(video, clip, youtube.make_hashtags(clip))
        status = "posted" if public else "private"
    except Exception as exc:
        db.set_tiktok(clip.id, "failed")
        if not tiktok_needs_audit(exc):
            video.unlink(missing_ok=True)
            raise
        try:
            publish_id, status = tiktok.upload_draft(video), "draft"
        except Exception:
            log.exception("TikTok-concept mislukt")
            raise exc  # keep the video: the bot sends it to post by hand
    db.set_tiktok(clip.id, status, publish_id)
    video.unlink(missing_ok=True)
    return publish_id, status
