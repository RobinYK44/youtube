"""Big word-by-word captions, like the large clip channels use. Speech recognition runs on the PC itself
(faster-whisper, free). Without it installed the Shorts are simply made without captions."""
import functools
import logging
import re
from pathlib import Path

from .config import config

log = logging.getLogger("shortsbot")
MAX_WORDS = 3  # words on screen at the same time
MAX_CHUNK_SECONDS = 1.6
# Burned-in swear words can limit ads on YouTube, so they get masked on screen (the audio stays as it is).
SWEARS = re.compile(r"\b(fuck\w*|shit\w*|bitch\w*|nigg\w*|cunt\w*|dick\w*|pussy\w*|motherfuck\w*)\b", re.IGNORECASE)


def available() -> bool:
    if not config.captions:
        return False
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


@functools.cache
def _model():
    from faster_whisper import WhisperModel

    log.info("Spraakherkenning laden (%s), de eerste keer wordt het model gedownload...", config.caption_model)
    return WhisperModel(config.caption_model, device="cpu", compute_type="int8")


def words(source: Path, start: float, length: float) -> list[tuple[float, float, str]]:
    """(start, end, word) of everything said in [start, start + length] of the video, times relative to start."""
    segments, _ = _model().transcribe(
        str(source), language="en", word_timestamps=True, vad_filter=True, condition_on_previous_text=False
    )
    found = []
    for segment in segments:
        for w in segment.words or []:
            if w.end <= start or w.start >= start + length:
                continue
            text = w.word.strip()
            if text:
                found.append((max(0.0, w.start - start), min(length, w.end - start), text))
    return found


def _clean(text: str) -> str:
    text = SWEARS.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), text)
    return re.sub(r"[^\w'!?*,. -]", "", text).upper().strip(" ,.")


def chunks(found: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Group words into short lines of at most MAX_WORDS, each shown until the next one starts."""
    lines: list[list] = []
    for start, end, word in found:
        current = lines[-1] if lines else None
        if (
            current
            and len(current[2]) < MAX_WORDS
            and start - current[1] < 0.5
            and end - current[0] <= MAX_CHUNK_SECONDS
            and not current[2][-1].endswith((".", "?", "!"))
        ):
            current[1] = end
            current[2].append(word)
        else:
            lines.append([start, end, [word]])
    result = []
    for i, (start, end, line_words) in enumerate(lines):
        if i + 1 < len(lines) and lines[i + 1][0] - end < 0.4:
            end = lines[i + 1][0]  # no flicker between lines that follow each other
        text = _clean(" ".join(line_words))
        if text:
            result.append((round(start, 2), round(end, 2), text))
    return result


def filters(source: Path, start: float, length: float, work_dir: Path, font: str, y: int) -> list[str]:
    """ffmpeg drawtext filters that show the captions. Empty when captions are off or nothing was said."""
    if not available():
        return []
    from .editor import _filter_path

    try:
        lines = chunks(words(source, start, length))
    except Exception:
        log.exception("Ondertitels maken mislukt, de short komt er zonder")
        return []
    result = []
    for i, (begin, end, text) in enumerate(lines):
        text_file = work_dir / f"caption{i}.txt"
        text_file.write_text(text, encoding="utf-8")
        color = "0xFFE500" if i % 2 else "white"  # alternate yellow and white, like the big clip channels
        size = max(48, min(82, int(1000 / (0.7 * len(text)))))  # long words must still fit on the screen
        result.append(
            f"drawtext=fontfile={_filter_path(font)}:textfile={_filter_path(text_file)}"
            f":fontsize={size}:fontcolor={color}:borderw=8:bordercolor=black"
            f":x=(w-text_w)/2:y={y}:enable='between(t,{begin},{end})'"
        )
    return result
