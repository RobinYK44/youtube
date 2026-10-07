"""Download a clip and turn it into a vertical 1080x1920 YouTube Short with ffmpeg."""
import functools
import logging
import re
import shutil
import subprocess
import tempfile
import textwrap
import unicodedata
from pathlib import Path

import yt_dlp

from . import captions, layout
from .config import config

log = logging.getLogger("shortsbot")
HOOK_SECONDS = 3.5  # filled screen: how long the title shows at the start

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]


@functools.cache
def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


@functools.cache
def _has_drawtext() -> bool:
    """Some ffmpeg builds (like the one in imageio-ffmpeg) lack the text filter."""
    result = subprocess.run([ffmpeg_exe(), "-hide_banner", "-filters"], capture_output=True, text=True)
    return " drawtext " in result.stdout


def _font() -> str | None:
    if not _has_drawtext():
        return None
    for candidate in [config.font_path, *FONT_CANDIDATES]:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _filter_path(path: Path | str) -> str:
    """Quote a file path for use inside an ffmpeg filtergraph option."""
    return "'" + Path(path).as_posix().replace(":", "\\:").replace("'", "") + "'"


def download(url: str, dest_dir: Path) -> Path:
    opts = {
        "outtmpl": str(dest_dir / "source.%(ext)s"),
        "format": "best[ext=mp4][vcodec!=?none]/best[vcodec!=?none]/best",  # never sound only
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": ffmpeg_exe(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info))


def _duration(video: Path) -> float:
    result = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(video)], capture_output=True, text=True)
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", result.stderr)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _check_picture(video: Path) -> None:
    """A download with only sound makes ffmpeg fail with an unreadable error; say what is wrong instead."""
    result = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(video)], capture_output=True, text=True)
    if "Video:" not in result.stderr:
        raise RuntimeError("de download had alleen geluid en geen beeld (YouTube/Twitch gaf geen bruikbare video)")


def _end_card(font: str, work_dir: Path, length: float) -> list[str]:
    """'LIKE & SUBSCRIBE' that fades in during the last 3 seconds, below the clip."""
    start = max(0.0, length - 3)
    fade = f"if(lt(t,{start}),0,min(1,(t-{start})/0.4))"
    cards = [
        ("LIKE & SUBSCRIBE", 76, 1400, ":box=1:boxcolor=0xE62117:boxborderw=22"),
        ("for more clips!", 48, 1515, ":borderw=4:bordercolor=black"),
    ]
    filters = []
    for i, (text, size, y, style) in enumerate(cards):
        text_file = work_dir / f"endcard{i}.txt"
        text_file.write_text(text, encoding="utf-8")
        filters.append(
            f"drawtext=fontfile={_filter_path(font)}:textfile={_filter_path(text_file)}"
            f":fontsize={size}:fontcolor=white{style}:x=(w-text_w)/2:y={y}"
            f":enable='gte(t,{start})':alpha='{fade}'"
        )
    return filters


def _plain(text: str) -> str:
    """Drop emoji and other symbols: the video font cannot draw them and shows empty boxes instead."""
    kept = "".join(
        ch for ch in text
        if ord(ch) <= 0xFFFF and unicodedata.category(ch) not in ("So", "Cs", "Co") and ch not in "\ufe0f\u200d"
    )
    return " ".join(kept.split())


def _cut(duration: float, limit: int | None = None) -> tuple[float, float]:
    """(start, length) of the part to keep. Long clips keep their last `limit` seconds, because clips
    are made right after something happened, so the moment is near the end."""
    limit = min(limit or config.target_short_seconds, config.max_short_seconds)
    if duration <= 0:
        return 0.0, float(config.max_short_seconds)
    if duration <= limit + 5:  # a few seconds over is fine, cutting them helps nobody
        return 0.0, min(duration, config.max_short_seconds)
    return duration - limit, float(limit)


def render_short(
    source: Path, output: Path, title: str, credit: str, work_dir: Path,
    limit: int | None = None, end_card: bool = True, smart_layout: bool = True,
) -> Path:
    font = _font()
    _check_picture(source)
    start, length = _cut(_duration(source), limit)
    shape = layout.choose(source, start, length) if smart_layout else layout.Layout("full")
    log.info("Layout: %s", shape.kind)
    full = shape.kind == "full"
    overlays = []
    if font:
        lines = textwrap.wrap(_plain(title), width=22)[:3]
        if full:  # the title above the clip, the whole time
            texts = [(line, 64, 200 + i * 80, "") for i, line in enumerate(lines)]
        else:  # the screen is filled: the title only as a hook in the first seconds, so it does not hide the face
            texts = [(line, 60, 90 + i * 78, f":box=1:boxcolor=black@0.55:boxborderw=14:enable='lt(t,{HOOK_SECONDS})'")
                     for i, line in enumerate(lines)]
        if credit:
            texts.append((credit, 44 if full else 36, "h-320" if full else "h-130", ""))
        for i, (text, size, y, extra) in enumerate(texts):
            text_file = work_dir / f"text{i}.txt"
            text_file.write_text(text, encoding="utf-8")
            overlays.append(
                f"drawtext=fontfile={_filter_path(font)}:textfile={_filter_path(text_file)}"
                f":fontsize={size}:fontcolor=white:borderw=5:bordercolor=black"
                f":x=(w-text_w)/2:y={y}{extra}"
            )
        # Captions just below the clip, on the line between facecam and game, or low on a filled screen.
        caption_y = {"full": 1290, "split": layout.TOP_HEIGHT - 60, "fill": 1250}[shape.kind]
        overlays += captions.filters(source, start, length, work_dir, font, y=caption_y)
        if end_card:
            overlays += _end_card(font, work_dir, length)

    graph = layout.video_filter(shape) + "".join("," + o for o in overlays) + ",format=yuv420p[v]"
    cmd = [
        ffmpeg_exe(), "-y", "-loglevel", "error",
        "-ss", f"{start:.2f}", "-i", str(source),
        "-filter_complex", graph,
        "-map", "[v]", "-map", "0:a?",
        "-t", f"{length:.2f}",
        "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg mislukt: {result.stderr.strip()[-800:]}")
    return output


def make_compilation(clip, output_dir: Path) -> Path:
    """Several clips in one Short, counting down: #3, #2, #1 (the best one last)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{clip.id}.mp4"
    total = len(clip.parts)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        segments = []
        for i, part in enumerate(clip.parts):
            part_dir = work / f"part{i}"
            part_dir.mkdir()
            source = download(part.url, part_dir)
            segment = work / f"segment{i}.mp4"
            render_short(
                source, segment, f"#{total - i}  {part.title}", f"twitch.tv/{part.broadcaster_login}", part_dir,
                limit=config.compilation_part_seconds, end_card=i == total - 1,
            )
            segments.append(segment)
        playlist = work / "segments.txt"
        playlist.write_text("".join(f"file '{s.as_posix()}'\n" for s in segments), encoding="utf-8")
        cmd = [
            ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(playlist),
            "-c", "copy", "-movflags", "+faststart", str(output),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg mislukt: {result.stderr.strip()[-800:]}")
    return output


def make_preview(video: Path, max_bytes: int = 9_500_000) -> Path | None:
    """Small copy that fits Discord's upload limit, so the Short can be watched in Discord."""
    preview = video.with_name(video.stem + "_preview.mp4")
    cmd = [
        ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(video),
        "-vf", "scale=360:-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(preview),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not preview.exists() or preview.stat().st_size > max_bytes:
        preview.unlink(missing_ok=True)
        return None
    return preview


def make_tiktok_version(video: Path, max_bytes: int = 9_500_000) -> Path | None:
    """720x1280 copy under Discord's 10 MB limit, to save on a phone and post to TikTok by hand."""
    out = video.with_name(video.stem + "_tiktok.mp4")
    for crf, audio in ((24, "128k"), (29, "96k"), (33, "64k")):
        cmd = [
            ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(video),
            "-vf", "scale=720:1280", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-c:a", "aac", "-b:a", audio, "-movflags", "+faststart", str(out),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and out.exists() and out.stat().st_size <= max_bytes:
            return out
    out.unlink(missing_ok=True)
    return None


def make_short(clip, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{clip.id}.mp4"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        source = download(clip.url, work)
        render_short(source, output, clip.title, f"twitch.tv/{clip.broadcaster_login}", work)
    return output


def make_youtube_short(clip, output_dir: Path) -> Path:
    """A moment from a YouTube video. Vyro clips get no end card or extra text: campaigns forbid that."""
    from . import ytclips

    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{clip.id}.mp4"
    vyro = clip.source == "vyro"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        source = ytclips.download(clip, work, ffmpeg_exe())
        credit = "" if vyro else f"youtube.com/@{clip.broadcaster_login}"
        render_short(
            source, output, clip.title, credit, work, limit=config.max_short_seconds, end_card=not vyro,
            smart_layout=not vyro,
        )
    return output
