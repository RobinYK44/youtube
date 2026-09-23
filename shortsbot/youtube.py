"""YouTube upload via the official Data API v3.

One-time login (run on a PC with a browser):
    python -m shortsbot.youtube auth
This creates youtube_token.json; copy it to wherever the bot runs.
"""
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import config

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def authorize() -> None:
    flow = InstalledAppFlow.from_client_secrets_file(str(config.youtube_client_secrets), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    config.youtube_token_file.write_text(creds.to_json(), encoding="utf-8")
    print(f"Klaar! Token opgeslagen in {config.youtube_token_file}")


def _credentials() -> Credentials:
    if not config.youtube_token_file.exists():
        raise RuntimeError("Geen YouTube-login gevonden. Draai eerst: python -m shortsbot.youtube auth")
    creds = Credentials.from_authorized_user_file(str(config.youtube_token_file), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        config.youtube_token_file.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_metadata(clip) -> dict:
    hashtag = "".join(ch for ch in clip.broadcaster_name if ch.isalnum())
    suffix = f" | {clip.broadcaster_name} #shorts"
    title = clip.title.strip() or f"{clip.broadcaster_name} moment"
    title = title[: 100 - len(suffix)].rstrip() + suffix
    description = (
        f"{clip.title}\n\n"
        f"Credits: {clip.broadcaster_name} — https://twitch.tv/{clip.broadcaster_login}\n"
        f"Originele clip: {clip.url}\n\n"
        f"#shorts #{hashtag} #twitch #streamer #clips"
    )
    tags = [clip.broadcaster_name, clip.broadcaster_login, "shorts", "twitch", "streamer", "clips", "funny"]
    return {"title": title, "description": description, "tags": tags}


def upload(video_path, clip) -> str:
    youtube = build("youtube", "v3", credentials=_credentials(), cache_discovery=False)
    meta = build_metadata(clip)
    body = {
        "snippet": {**meta, "categoryId": "20"},  # 20 = Gaming
        "status": {"privacyStatus": config.youtube_privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]


if __name__ == "__main__":
    if sys.argv[1:] == ["auth"]:
        authorize()
    else:
        print("Gebruik: python -m shortsbot.youtube auth")
