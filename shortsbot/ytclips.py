"""Clips from YouTube videos: find the moments people re-watch most and cut them out.

YouTube shows a "most replayed" graph above the timeline of popular videos; yt-dlp reads it as `heatmap`.
The peaks in that graph are almost always the funniest or craziest moments.
"""
import logging
from datetime import datetime, timezone
from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func

from .twitch import Clip

log = logging.getLogger("shortsbot")
MOMENT_SECONDS = 30
BEFORE_PEAK = 10  # start a bit before the most re-watched point, so viewers get the build-up
SKIP_START = 0.05  # everybody watches the first seconds, so the start of the graph says nothing
HEAT_SCORE = 4000  # scale a moment's heat (0-1) to the same range as a good Twitch clip's viral score


class _Silent:
    """yt-dlp prints errors itself even when we catch them (like 'no streams tab'). Errors still raise."""

    def debug(self, msg):
        pass

    info = warning = error = debug


def _ydl(**opts) -> yt_dlp.YoutubeDL:
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "logger": _Silent(), **opts})


def channel_url(channel: str) -> str:
    """'MrBeast', '@MrBeast' or a link -> https://www.youtube.com/@MrBeast"""
    channel = channel.strip()
    if channel.startswith("http"):
        return channel.rstrip("/")
    return "https://www.youtube.com/@" + channel.lstrip("@")


def latest_videos(channel: str, per_tab: int = 4) -> list[str]:
    """Links to the newest long videos and past live streams of a channel."""
    urls = []
    for tab in ("videos", "streams"):
        try:
            with _ydl(extract_flat="in_playlist", playlistend=per_tab) as ydl:
                info = ydl.extract_info(f"{channel_url(channel)}/{tab}", download=False)
        except Exception:
            continue  # not every channel has a streams tab
        urls += [f"https://www.youtube.com/watch?v={e['id']}" for e in info.get("entries") or [] if e.get("id")]
    return urls


def video_info(url: str) -> dict:
    with _ydl(skip_download=True) as ydl:
        return ydl.extract_info(url, download=False)


def age_days(info: dict) -> float:
    timestamp = info.get("timestamp") or info.get("release_timestamp")
    if not timestamp:
        date = info.get("upload_date")  # YYYYMMDD
        if not date:
            return 0.0
        timestamp = datetime.strptime(date, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
    return max(0.0, (datetime.now(timezone.utc).timestamp() - timestamp) / 86400)


def moments(info: dict, count: int = 3, length: int = MOMENT_SECONDS) -> list[tuple[float, float]]:
    """(start, heat) of the `count` most re-watched moments, far enough apart, best first."""
    duration = info.get("duration") or 0
    heatmap = info.get("heatmap") or []
    if not heatmap or duration < length * 2:
        return []
    peaks = sorted(
        (p for p in heatmap if p["start_time"] >= duration * SKIP_START), key=lambda p: p["value"], reverse=True
    )
    chosen: list[tuple[float, float]] = []
    for peak in peaks:
        centre = (peak["start_time"] + peak["end_time"]) / 2
        start = round(min(max(0.0, centre - BEFORE_PEAK), duration - length), 1)
        if all(abs(start - other) >= length for other, _ in chosen):
            chosen.append((start, peak["value"]))
        if len(chosen) == count:
            break
    return chosen


def make_clip(info: dict, start: float, heat: float, source: str = "youtube", tags: list[str] | None = None) -> Clip:
    handle = (info.get("uploader_id") or info.get("channel") or "youtube").lstrip("@")
    timestamp = info.get("timestamp") or datetime.now(timezone.utc).timestamp()
    return Clip(
        id=f"yt-{info['id']}-{int(start)}",
        url=f"https://www.youtube.com/watch?v={info['id']}&t={int(start)}s",
        title=info.get("title") or "",
        broadcaster_login=handle.lower(),
        broadcaster_name=info.get("channel") or info.get("uploader") or handle,
        view_count=info.get("view_count") or 0,
        duration=float(MOMENT_SECONDS),
        created_at=datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        score=round(heat * HEAT_SCORE, 1),
        source=source,
        start=start,
        tags=tags or [],
    )


def download(clip: Clip, dest_dir: Path, ffmpeg: str) -> Path:
    """Download only the moment itself, not the whole video."""
    opts = {
        "outtmpl": str(dest_dir / "source.%(ext)s"),
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b",
        "merge_output_format": "mp4",
        "download_ranges": download_range_func(None, [(clip.start, clip.start + clip.duration)]),
        "force_keyframes_at_cuts": True,
        "ffmpeg_location": ffmpeg,
    }
    with _ydl(**opts) as ydl:
        ydl.extract_info(clip.url.split("&t=")[0], download=True)
    files = sorted(dest_dir.glob("source.*"), key=lambda f: f.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("YouTube-video downloaden mislukt")
    return files[0]
