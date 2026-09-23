import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "ja")


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _list(name: str, default: str = "") -> list[str]:
    return [s.strip().lower() for s in os.getenv(name, default).split(",") if s.strip()]


DEFAULT_STREAMERS = "jynxzi,kaicenat,caseoh_,xqc,stableronaldo,lacy,fanum,adapt"


@dataclass
class Config:
    discord_token: str = os.getenv("DISCORD_TOKEN", "")
    discord_channel_id: int = _int("DISCORD_CHANNEL_ID", 0)

    twitch_client_id: str = os.getenv("TWITCH_CLIENT_ID", "")
    twitch_client_secret: str = os.getenv("TWITCH_CLIENT_SECRET", "")

    youtube_client_secrets: Path = ROOT / os.getenv("YOUTUBE_CLIENT_SECRETS", "client_secret.json")
    youtube_token_file: Path = ROOT / os.getenv("YOUTUBE_TOKEN_FILE", "youtube_token.json")
    youtube_privacy: str = os.getenv("YOUTUBE_PRIVACY", "public")

    streamers: list[str] = field(default_factory=lambda: _list("STREAMERS", DEFAULT_STREAMERS))
    auto_discover_top: int = _int("AUTO_DISCOVER_TOP", 5)
    discover_language: str = os.getenv("DISCOVER_LANGUAGE", "en").strip()

    uploads_per_day: int = _int("UPLOADS_PER_DAY", 4)
    approval_mode: bool = _bool("APPROVAL_MODE", False)

    clip_lookback_days: int = _int("CLIP_LOOKBACK_DAYS", 2)
    min_clip_views: int = _int("MIN_CLIP_VIEWS", 500)
    min_clip_seconds: int = _int("MIN_CLIP_SECONDS", 10)
    max_short_seconds: int = _int("MAX_SHORT_SECONDS", 60)
    font_path: str = os.getenv("FONT_PATH", "").strip()

    def missing(self) -> list[str]:
        required = {
            "DISCORD_TOKEN": self.discord_token,
            "DISCORD_CHANNEL_ID": self.discord_channel_id,
            "TWITCH_CLIENT_ID": self.twitch_client_id,
            "TWITCH_CLIENT_SECRET": self.twitch_client_secret,
        }
        return [name for name, value in required.items() if not value]


config = Config()
DATA_DIR.mkdir(exist_ok=True)
