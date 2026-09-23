"""Twitch Helix API: find the biggest live streamers and their most-viewed clips."""
import time
from dataclasses import dataclass
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

    def user_ids(self, logins: list[str]) -> dict[str, tuple[str, str]]:
        """Map login -> (user id, display name). Unknown logins are skipped."""
        result = {}
        for i in range(0, len(logins), 100):
            chunk = logins[i : i + 100]
            for u in self._get("users", [("login", login) for login in chunk]):
                result[u["login"]] = (u["id"], u["display_name"])
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
            )
            for c in data
        ]
