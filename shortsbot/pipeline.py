"""Glue: pick streamers -> find the best unused clip -> render -> upload."""
import logging
from pathlib import Path

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
    candidates.sort(key=lambda c: c.view_count, reverse=True)
    return candidates


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


def render(clip: Clip) -> Path:
    return editor.make_short(clip, OUTPUT_DIR)


def publish(clip: Clip, video: Path) -> str:
    try:
        video_id = youtube.upload(video, clip)
    except Exception:
        db.mark(clip, "failed")
        raise
    db.mark(clip, "uploaded", video_id)
    video.unlink(missing_ok=True)
    return video_id
