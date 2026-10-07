"""How a clip fills the phone screen. Finds the streamer's face (OpenCV, free, on the PC itself):

- 'split': a facecam in a corner -> the facecam on top, the game below, the whole screen filled.
- 'fill':  the streamer fills the stream (Just Chatting, IRL) -> a tall cut around the face, full screen.
- 'full':  no face found -> the whole clip in the middle, nothing cut off (so a missed facecam is never lost).

Without OpenCV installed every clip gets 'full', like before.
"""
import logging
from dataclasses import dataclass
from pathlib import Path

from .config import config

log = logging.getLogger("shortsbot")
SAMPLES = 8  # frames looked at per clip
TOP_HEIGHT = 640  # split: facecam area on top; the game gets the other 1280 pixels
SAME_PLACE = 0.08  # faces this close (part of the width) in different frames are the same face
MIN_SHARE = 0.5  # the face must be in at least half of the frames: a streamer, not a game character


@dataclass
class Layout:
    kind: str  # 'split', 'fill' or 'full'
    width: int = 1920
    height: int = 1080
    cam: tuple[int, int, int, int] | None = None  # split: x, y, w, h of the facecam in the clip
    crop_x: int = 0  # fill: left side of the tall cut


def _even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def _faces(source: Path, start: float, length: float) -> tuple[int, int, list[list[tuple[float, ...]]]]:
    import cv2

    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    video = cv2.VideoCapture(str(source))
    width = int(video.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(video.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frames = []
    try:
        for i in range(SAMPLES):
            video.set(cv2.CAP_PROP_POS_MSEC, (start + length * (i + 0.5) / SAMPLES) * 1000)
            ok, frame = video.read()
            if not ok or not width:
                continue
            scale = width / 640
            small = cv2.resize(frame, (640, int(height / scale)))
            gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
            found = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(22, 22))
            frames.append([tuple(v * scale for v in face) for face in found])
    finally:
        video.release()
    return width, height, frames


def _steady_face(width: int, frames: list[list[tuple[float, ...]]]) -> tuple[float, ...] | None:
    """The face that stays in the same place in most frames (x, y, w, h, averaged), or None."""
    best, best_count = None, 0
    for face in (f for faces in frames for f in faces):
        cx, cy = face[0] + face[2] / 2, face[1] + face[3] / 2
        matches = []
        for faces in frames:
            near = [
                f for f in faces
                if abs(f[0] + f[2] / 2 - cx) < SAME_PLACE * width
                and abs(f[1] + f[3] / 2 - cy) < SAME_PLACE * width
                and 0.6 < f[2] / face[2] < 1.6
            ]
            if near:
                matches.append(near[0])
        if len(matches) > best_count:
            best_count = len(matches)
            best = tuple(sum(m[i] for m in matches) / len(matches) for i in range(4))
    if best is None or best_count < max(3, MIN_SHARE * len(frames)):
        return None
    return best


def choose(source: Path, start: float, length: float) -> Layout:
    try:
        width, height, frames = _faces(source, start, length)
    except ImportError:
        return Layout("full")
    except Exception:
        log.exception("Gezicht zoeken mislukt, de clip komt er heel in")
        return Layout("full")
    if not width or not height:
        return Layout("full")
    face = _steady_face(width, frames)
    if face is None:
        return Layout("full", width, height)
    x, y, w, h = face
    cx, cy = x + w / 2, y + h / 2
    if width * 0.3 < cx < width * 0.7 and w > width * 0.06:
        # The streamer in the middle of the stream: cut a tall strip around the face.
        strip = height * 9 / 16
        left = min(max(0, cx - strip / 2), width - strip)
        return Layout("fill", width, height, crop_x=_even(left))
    if w > width * 0.2:
        return Layout("full", width, height)  # a big face at the side: not a facecam, keep everything
    # A facecam: a box around the face with the shape of the top area, the face a bit above the middle.
    box_w = min(width, w * 3.2)
    box_h = min(height, box_w * TOP_HEIGHT / 1080)
    box_w = box_h * 1080 / TOP_HEIGHT
    left = min(max(0, cx - box_w / 2), width - box_w)
    top = min(max(0, cy - box_h * 0.45), height - box_h)
    return Layout("split", width, height, cam=(_even(left), _even(top), _even(box_w), _even(box_h)))


def video_filter(layout: Layout) -> str:
    """ffmpeg filter from input [0:v] to a 1080x1920 picture (without the text on it), ending in a label-less
    chain so the caller can add overlays."""
    if layout.kind == "split":
        x, y, w, h = layout.cam
        game_w = _even(layout.height * 1080 / (1920 - TOP_HEIGHT))
        game_x = _even((layout.width - game_w) / 2)
        return (
            "[0:v]split[cam][game];"
            f"[cam]crop={w}:{h}:{x}:{y},scale=1080:{TOP_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop=1080:{TOP_HEIGHT},setsar=1[top];"
            f"[game]crop={game_w}:{layout.height}:{game_x}:0,scale=1080:{1920 - TOP_HEIGHT}:"
            f"force_original_aspect_ratio=increase,crop=1080:{1920 - TOP_HEIGHT},setsar=1[bottom];"
            "[top][bottom]vstack=inputs=2"
        )
    if layout.kind == "fill":
        strip = _even(layout.height * 9 / 16)
        return f"[0:v]crop={strip}:{layout.height}:{layout.crop_x}:0,scale=1080:1920,setsar=1"
    return (
        "[0:v]split[a][b];"
        "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "boxblur=20:5,eq=brightness=-0.15[bg];"
        f"[b]crop=iw/{config.clip_zoom}:ih,scale=1080:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )
