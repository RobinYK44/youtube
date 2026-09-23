"""Download a clip and turn it into a vertical 1080x1920 YouTube Short with ffmpeg."""
import functools
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import yt_dlp

from .config import config

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
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": ffmpeg_exe(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info))


def render_short(source: Path, output: Path, title: str, credit: str, work_dir: Path) -> Path:
    font = _font()
    overlays = []
    if font:
        lines = textwrap.wrap(title, width=22)[:3]
        texts = [(line, 64, 200 + i * 80) for i, line in enumerate(lines)]
        texts.append((credit, 44, "h-320"))
        for i, (text, size, y) in enumerate(texts):
            text_file = work_dir / f"text{i}.txt"
            text_file.write_text(text, encoding="utf-8")
            overlays.append(
                f"drawtext=fontfile={_filter_path(font)}:textfile={_filter_path(text_file)}"
                f":fontsize={size}:fontcolor=white:borderw=5:bordercolor=black"
                f":x=(w-text_w)/2:y={y}"
            )

    graph = (
        "[0:v]split[a][b];"
        "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "boxblur=20:5,eq=brightness=-0.15[bg];"
        "[b]scale=1080:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2" + "".join("," + o for o in overlays) + ",format=yuv420p[v]"
    )
    cmd = [
        ffmpeg_exe(), "-y", "-loglevel", "error",
        "-i", str(source),
        "-filter_complex", graph,
        "-map", "[v]", "-map", "0:a?",
        "-t", str(config.max_short_seconds),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg mislukt: {result.stderr.strip()[-800:]}")
    return output


def make_short(clip, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{clip.id}.mp4"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        source = download(clip.url, work)
        render_short(source, output, clip.title, f"twitch.tv/{clip.broadcaster_login}", work)
    return output
