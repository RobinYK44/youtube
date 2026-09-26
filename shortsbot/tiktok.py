"""TikTok upload via the official Content Posting API (Direct Post).

One-time login (run on a PC with a browser):
    python -m shortsbot.tiktok auth
This creates tiktok_token.json. Until TikTok audits the app, posts are only visible to you (SELF_ONLY).
"""
import hashlib
import json
import math
import secrets
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from .config import config

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,video.publish,video.upload"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback/"
MIN_CHUNK, MAX_CHUNK = 5 * 1024 * 1024, 64 * 1024 * 1024


class TikTokError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(config.tiktok_client_key and config.tiktok_client_secret and config.tiktok_token_file.exists())


# ---- login -------------------------------------------------------------------------------------------

def _save(token: dict) -> None:
    token["expires_at"] = time.time() + token.get("expires_in", 0)
    config.tiktok_token_file.write_text(json.dumps(token, indent=2), encoding="utf-8")


def _token_request(data: dict) -> dict:
    r = requests.post(
        f"{API}/oauth/token/",
        data={"client_key": config.tiktok_client_key, "client_secret": config.tiktok_client_secret, **data},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    body = r.json()
    if "access_token" not in body:
        raise TikTokError(f"TikTok-login mislukt: {body.get('error_description') or body}")
    return body


def authorize() -> None:
    if not (config.tiktok_client_key and config.tiktok_client_secret):
        sys.exit("Vul eerst TIKTOK_CLIENT_KEY en TIKTOK_CLIENT_SECRET in je .env-bestand in.")
    verifier = secrets.token_urlsafe(64)[:64]
    state = secrets.token_urlsafe(16)
    params = {
        "client_key": config.tiktok_client_key,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "code_challenge": hashlib.sha256(verifier.encode()).hexdigest(),  # TikTok desktop wants hex
        "code_challenge_method": "S256",
    }
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in query.items()})
            ok = "code" in result and result.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            text = "Gelukt! Je kunt dit tabblad sluiten." if ok else "Inloggen mislukt, kijk in het zwarte venster."
            self.wfile.write(f"<h1 style='font-family:sans-serif'>{text}</h1>".encode())

        def log_message(self, *args):
            pass

    from .youtube import _open_login_page  # same trick: long URLs break when copied from a terminal

    server = HTTPServer(("localhost", REDIRECT_PORT), Callback)
    _open_login_page(f"{AUTH_URL}?{urllib.parse.urlencode(params)}", "TikTok")
    while "code" not in result and "error" not in result:
        server.handle_request()
    server.server_close()
    if result.get("state") != state or "code" not in result:
        sys.exit(f"TikTok-login mislukt: {result.get('error_description') or result.get('error') or result}")

    token = _token_request({
        "code": result["code"],
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    })
    _save(token)
    print(f"Klaar! TikTok-token opgeslagen in {config.tiktok_token_file}")


def _access_token() -> str:
    token = json.loads(config.tiktok_token_file.read_text(encoding="utf-8"))
    if time.time() > token.get("expires_at", 0) - 300:
        try:
            token = _token_request({"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
        except TikTokError as exc:
            raise TikTokError(f"TikTok-login is verlopen. Dubbelklik op `tiktok_login` op je pc. ({exc})")
        _save(token)
    return token["access_token"]


# ---- posting -----------------------------------------------------------------------------------------

def _post(path: str, body: dict) -> dict:
    r = requests.post(
        f"{API}{path}",
        headers={"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json; charset=UTF-8"},
        json=body,
        timeout=60,
    )
    payload = r.json()
    error = payload.get("error", {})
    if error.get("code", "ok") != "ok":
        raise TikTokError(f"{error.get('code')}: {error.get('message')}")
    return payload.get("data", {})


def _chunks(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) following TikTok's rules: one piece up to 64 MB."""
    if size <= MAX_CHUNK:
        return size, 1
    chunk = 10 * 1024 * 1024
    return chunk, size // chunk  # the last chunk takes the remaining bytes


def caption(clip, hashtags: list[str]) -> str:
    from .youtube import tiktok_caption

    return tiktok_caption(clip)


def _send_file(upload_url: str, video_path, size: int, chunk_size: int, count: int) -> None:
    with open(video_path, "rb") as f:
        for i in range(count):
            start = i * chunk_size
            end = size - 1 if i == count - 1 else start + chunk_size - 1
            f.seek(start)
            piece = f.read(end - start + 1)
            r = requests.put(
                upload_url,
                data=piece,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(piece)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                },
                timeout=300,
            )
            if r.status_code not in (200, 201, 206):
                raise TikTokError(f"Uploaden naar TikTok mislukt (HTTP {r.status_code}): {r.text[:300]}")


def _wait(publish_id: str, done: str) -> None:
    for _ in range(40):  # wait up to ~4 minutes for TikTok to process the video
        status = _post("/post/publish/status/fetch/", {"publish_id": publish_id})
        if status.get("status") == done:
            return
        if status.get("status") == "FAILED":
            raise TikTokError(f"TikTok weigerde de video: {status.get('fail_reason')}")
        time.sleep(6)


def upload(video_path, clip, hashtags: list[str]) -> tuple[str, bool]:
    """Post a video. Returns (publish_id, public). Falls back to private while the app is not audited."""
    info = _post("/post/publish/creator_info/query/", {})
    options = info.get("privacy_level_options") or ["SELF_ONLY"]
    privacy = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in options else options[-1]

    size = video_path.stat().st_size
    chunk_size, count = _chunks(size)

    def init(level: str) -> dict:
        return _post("/post/publish/video/init/", {
            "post_info": {
                "title": caption(clip, hashtags),
                "privacy_level": level,
                "disable_duet": bool(info.get("duet_disabled")),
                "disable_comment": bool(info.get("comment_disabled")),
                "disable_stitch": bool(info.get("stitch_disabled")),
                "video_cover_timestamp_ms": 1000,
            },
            "source_info": {
                "source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size, "total_chunk_count": count,
            },
        })

    try:
        data = init(privacy)
    except TikTokError as exc:
        if "unaudited" not in str(exc) or privacy == "SELF_ONLY":
            raise
        privacy = "SELF_ONLY"  # not audited yet: TikTok only allows private posts
        data = init(privacy)

    _send_file(data["upload_url"], video_path, size, chunk_size, count)
    _wait(data["publish_id"], "PUBLISH_COMPLETE")
    return data["publish_id"], privacy == "PUBLIC_TO_EVERYONE"


def upload_draft(video_path) -> str:
    """Send a video to the TikTok app's inbox; the owner adds the text and posts it there. Returns publish_id."""
    size = video_path.stat().st_size
    chunk_size, count = _chunks(size)
    data = _post("/post/publish/inbox/video/init/", {
        "source_info": {
            "source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size, "total_chunk_count": count,
        },
    })
    _send_file(data["upload_url"], video_path, size, chunk_size, count)
    _wait(data["publish_id"], "SEND_TO_USER_INBOX")
    return data["publish_id"]


if __name__ == "__main__":
    if sys.argv[1:] == ["auth"]:
        authorize()
    else:
        print("Gebruik: python -m shortsbot.tiktok auth")
