"""YouTube upload via the official Data API v3.

One-time login (run on a PC with a browser):
    python -m shortsbot.youtube auth
This creates youtube_token.json; copy it to wherever the bot runs.
"""
import html
import os
import re
import sys
import webbrowser
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import DATA_DIR, config

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# Hashtags are guessed from the clip title, the streamer and the game: the bot cannot watch the video,
# so the mood (funny, scary, ...) comes from words in the title. General tags fill up the rest.
MOODS = {
    "funny": ["lol", "lmao", "lmfao", "funny", "haha", "joke", "prank", "laugh", "crying", "😂", "🤣", "💀"],
    "rage": ["rage", "mad", "angry", "tilt", "scream", "yell", "😡", "🤬"],
    "scary": ["scary", "scared", "horror", "jumpscare", "jump scare", "ghost", "creepy", "😱"],
    "clutch": ["clutch", "insane", "crazy", "cracked", "1v", "ace", "no way", "impossible", "goat", "🔥"],
    "fail": ["fail", "fell", "oops", "bruh", "rip", "died", "choke", "throw"],
    "wholesome": ["wholesome", "cute", "sweet", "love", "❤", "🥹"],
    "music": ["sing", "song", "music", "dance", "rap", "🎵", "🎶"],
}
FILLER = ["funny", "twitch", "streamer", "viral", "clips", "gaming"]


def _hashtag(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def moods(title: str) -> list[str]:
    lowered = title.lower()
    found = []
    for mood, words in MOODS.items():
        for word in words:
            # Whole words for letters ("rip" not in "trip"); emoji can be anywhere.
            pattern = rf"(?<!\w){re.escape(word)}" if word[0].isalnum() else re.escape(word)
            if re.search(pattern, lowered):
                found.append(mood)
                break
    return found


def make_hashtags(clip, minimum: int = 6, maximum: int = 8) -> list[str]:
    """Hashtags without '#', most specific first. Always starts with 'shorts'."""
    parts = getattr(clip, "parts", None) or [clip]
    found = list(dict.fromkeys(mood for part in parts for mood in moods(part.title)))
    if len(parts) > 1:
        found = list(dict.fromkeys(["funny", *found, "compilation"]))
    tags = [
        "shorts",
        *(_hashtag(part.broadcaster_name) for part in parts),
        *found,
        *(_hashtag(getattr(part, "game", "") or "") for part in parts),
    ]
    for filler in FILLER:
        if found and filler == "funny":
            continue  # a scary or rage clip should not also be called funny
        if len([t for t in dict.fromkeys(tags) if t]) >= minimum:
            break
        tags.append(filler)
    return [t for t in dict.fromkeys(tags) if t][:maximum]


def _open_login_page(url: str) -> None:
    """Open the login URL via a local HTML file: long URLs break when copied from a terminal."""
    page = DATA_DIR / "login.html"
    page.write_text(
        '<!doctype html><meta charset="utf-8"><title>Inloggen bij YouTube</title>'
        '<body style="font-family:sans-serif;text-align:center;margin-top:80px">'
        "<h1>Shortsbot: inloggen bij YouTube</h1>"
        f'<p><a href="{html.escape(url)}" style="font-size:24px">Klik hier om in te loggen</a></p>'
        f'<script>location.href = {url!r};</script>',
        encoding="utf-8",
    )
    print("Je browser opent nu het Google-inlogscherm.")
    print(f"Gebeurt er niks? Dubbelklik dan op dit bestand: {page}")
    try:
        if hasattr(os, "startfile"):
            os.startfile(page)
        else:
            webbrowser.open(page.as_uri())
    except Exception:
        pass


def authorize() -> None:
    secrets = config.youtube_client_secrets
    if not secrets.exists():
        doubled = secrets.with_name(secrets.name + ".json")
        hint = f" Het bestand heet nu {doubled.name}; hernoem het naar {secrets.name}." if doubled.exists() else ""
        sys.exit(f"Bestand {secrets.name} niet gevonden in {secrets.parent}.{hint}")

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    make_url = flow.authorization_url

    def authorization_url(**kwargs):
        url, state = make_url(**kwargs)
        _open_login_page(url)
        return url, state

    flow.authorization_url = authorization_url
    creds = flow.run_local_server(
        port=0,
        open_browser=False,
        authorization_prompt_message="",
        success_message="Gelukt! Je kunt dit tabblad sluiten en teruggaan naar het zwarte venster.",
        prompt="consent",
        access_type="offline",
    )
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
    hashtags = make_hashtags(clip)
    suffix = f" | {clip.broadcaster_name} #shorts"
    title = clip.title.strip() or f"{clip.broadcaster_name} moment"
    title = title[: 100 - len(suffix)].rstrip() + suffix
    if clip.parts:
        credits = "Credits:\n" + "".join(
            f"#{len(clip.parts) - i} {part.broadcaster_name} — https://twitch.tv/{part.broadcaster_login} ({part.url})\n"
            for i, part in enumerate(clip.parts)
        )
    else:
        credits = (
            f"Credits: {clip.broadcaster_name} — https://twitch.tv/{clip.broadcaster_login}\n"
            f"Originele clip: {clip.url}\n"
        )
    description = f"{clip.title}\n\n{credits}\n" + " ".join(f"#{tag}" for tag in hashtags)
    names = [part.broadcaster_name for part in clip.parts] or [clip.broadcaster_name, clip.broadcaster_login]
    tags = list(dict.fromkeys([*names, *hashtags, "twitch", "clips"]))
    if getattr(clip, "game", ""):
        tags.append(clip.game)
    return {"title": title, "description": description, "tags": tags}


def upload(video_path, clip, publish_at: datetime | None = None) -> str:
    """Upload a Short. With publish_at, YouTube keeps it private and publishes it at that time."""
    youtube = build("youtube", "v3", credentials=_credentials(), cache_discovery=False)
    meta = build_metadata(clip)
    status = {"privacyStatus": config.youtube_privacy, "selfDeclaredMadeForKids": False}
    if publish_at and config.youtube_privacy == "public":
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {"snippet": {**meta, "categoryId": "20"}, "status": status}  # 20 = Gaming
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
