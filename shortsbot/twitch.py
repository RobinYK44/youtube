"""Twitch Helix API: find the biggest live streamers and their most-viewed clips."""
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.twitch.tv/helix"


@dataclass
class Clip:
    id: str
    url: str
    title: str
    broadcaster_login: str
    broadcaster_name: str
    view_count: int
    duration: float
    created_at: str
    game_id: str = ""
    game: str = ""
    score: float = 0.0  # viral score, see pipeline.viral_score
    parts: list["Clip"] = field(default_factory=list)  # set for a compilation of several clips
    source: str = "twitch"  # twitch / youtube / vyro (a YouTube clip for a paid Vyro campaign)
    start: float = 0.0  # youtube: where the moment starts in the video
    tags: list[str] = field(default_factory=list)  # vyro: the campaign's hashtags, used instead of our own


class Twitch:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = ""
        self._token_expires = 0.0

    def _headers(self) -> dict:
        if time.time() > self._token_expires - 60:
            r = requests.post(
                "https://id.twitch.tv/oauth2/token",
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=20,
            )
            r.raise_for_status()
            body = r.json()
            self._token = body["access_token"]
            self._token_expires = time.time() + body["expires_in"]
        return {"Client-Id": self.client_id, "Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params) -> list[dict]:
        r = requests.get(f"{API}/{path}", headers=self._headers(), params=params, timeout=20)
        r.raise_for_status()
        return r.json()["data"]

    def top_live_streamers(self, count: int, language: str = "") -> list[dict]:
        """Current live streams sorted by viewer count (Twitch returns them sorted)."""
        params = {"first": min(max(count, 1), 100)}
        if language:
            params["language"] = language
        streams = self._get("streams", params)
        return [
            {"login": s["user_login"], "name": s["user_name"], "viewers": s["viewer_count"], "game": s["game_name"]}
            for s in streams[:count]
        ]

    def live_logins(self, user_ids: list[str]) -> set[str]:
        """Which of these streamers are live right now."""
        result = set()
        for i in range(0, len(user_ids), 100):
            params = [("user_id", uid) for uid in user_ids[i : i + 100]] + [("first", "100")]
            result |= {s["user_login"] for s in self._get("streams", params)}
        return result

    def clip_by_id(self, clip_id: str) -> Clip | None:
        """One clip by its id (the last part of a clip link)."""
        data = self._get("clips", {"id": clip_id})
        if not data:
            return None
        c = data[0]
        return Clip(
            id=c["id"],
            url=c["url"],
            title=c["title"],
            broadcaster_login=c["broadcaster_name"].lower(),
            broadcaster_name=c["broadcaster_name"],
            view_count=c["view_count"],
            duration=float(c["duration"]),
            created_at=c["created_at"],
            game_id=c.get("game_id", ""),
        )

    def user_ids(self, logins: list[str]) -> dict[str, tuple[str, str]]:
        """Map login -> (user id, display name). Unknown logins are skipped."""
        result = {}
        for i in range(0, len(logins), 100):
            chunk = logins[i : i + 100]
            for u in self._get("users", [("login", login) for login in chunk]):
                result[u["login"]] = (u["id"], u["display_name"])
        return result

    def game_names(self, game_ids: list[str]) -> dict[str, str]:
        """Map game id -> game name, e.g. '509658' -> 'Just Chatting'."""
        result = {}
        ids = [i for i in game_ids if i]
        for i in range(0, len(ids), 100):
            for g in self._get("games", [("id", gid) for gid in ids[i : i + 100]]):
                result[g["id"]] = g["name"]
        return result

    def top_clips(self, login: str, user_id: str, name: str, days: int, first: int = 20) -> list[Clip]:
        """Most-viewed clips of one streamer in the last `days` days."""
        now = datetime.now(timezone.utc)
        data = self._get(
            "clips",
            {
                "broadcaster_id": user_id,
                "started_at": (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ended_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "first": first,
            },
        )
        return [
            Clip(
                id=c["id"],
                url=c["url"],
                title=c["title"],
                broadcaster_login=login,
                broadcaster_name=name,
                view_count=c["view_count"],
                duration=float(c["duration"]),
                created_at=c["created_at"],
                game_id=c.get("game_id", ""),
            )
            for c in data
        ]
