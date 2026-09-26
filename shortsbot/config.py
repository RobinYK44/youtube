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


def _min_clip_views() -> int:
    value = _int("MIN_CLIP_VIEWS", 3000)
    return 3000 if value == 500 else value  # 500 was the old default copied into .env files: too many weak clips


def _list(name: str, default: str = "") -> list[str]:
    return [s.strip().lower() for s in os.getenv(name, default).split(",") if s.strip()]


DEFAULT_STREAMERS = "jynxzi,stableronaldo,lacy,marlon,kaicenat,caseoh_,xqc,fanum,adapt"


@dataclass
class Config:
    discord_token: str = os.getenv("DISCORD_TOKEN", "")
    discord_channel_id: int = _int("DISCORD_CHANNEL_ID", 0)

    twitch_client_id: str = os.getenv("TWITCH_CLIENT_ID", "")
    twitch_client_secret: str = os.getenv("TWITCH_CLIENT_SECRET", "")

    youtube_client_secrets: Path = ROOT / os.getenv("YOUTUBE_CLIENT_SECRETS", "client_secret.json")
    youtube_token_file: Path = ROOT / os.getenv("YOUTUBE_TOKEN_FILE", "youtube_token.json")
    youtube_privacy: str = os.getenv("YOUTUBE_PRIVACY", "public")

    tiktok_client_key: str = os.getenv("TIKTOK_CLIENT_KEY", "").strip()
    tiktok_client_secret: str = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()
    tiktok_token_file: Path = ROOT / os.getenv("TIKTOK_TOKEN_FILE", "tiktok_token.json")

    streamers: list[str] = field(default_factory=lambda: _list("STREAMERS", DEFAULT_STREAMERS))
    auto_discover_top: int = _int("AUTO_DISCOVER_TOP", 5)
    discover_language: str = os.getenv("DISCOVER_LANGUAGE", "en").strip()

    # Times (local, see TIMEZONE) at which YouTube publishes the Shorts. The bot uploads them in advance,
    # so they still go online when the computer is off.
    # Default: afternoon and evening in the US (Eastern time), where most viewers of English clips live.
    publish_times: list[str] = field(default_factory=lambda: _list("PUBLISH_TIMES", "18:00,21:00,00:00,02:00"))
    timezone: str = os.getenv("TIMEZONE", "Europe/Amsterdam").strip()
    # Kiesmodus: render this many Shorts a day and let the owner pick in Discord (0 = fully automatic).
    # Can also be changed from Discord with /kiesmodus.
    candidates_per_day: int = _int("CANDIDATES_PER_DAY", 0)

    clip_lookback_days: int = _int("CLIP_LOOKBACK_DAYS", 2)
    min_clip_views: int = _min_clip_views()
    # When the favourite streamers have too few new clips, look this far back (popular older clips still do well).
    clip_lookback_max_days: int = _int("CLIP_LOOKBACK_MAX_DAYS", 14)
    # YouTube channels to clip from (their newest videos and streams). Can be changed with /youtuber_toevoegen.
    youtube_channels: list[str] = field(default_factory=lambda: _list("YOUTUBE_CHANNELS", "ishowspeed,mrbeast"))
    clip_zoom: float = float(os.getenv("CLIP_ZOOM", "").strip() or 1.0)  # >1 crops the sides, and the facecam with them
    min_clip_seconds: int = _int("MIN_CLIP_SECONDS", 10)
    max_short_seconds: int = _int("MAX_SHORT_SECONDS", 60)
    # Longer clips are cut to about this length. The end is kept: clips are made right after the moment.
    target_short_seconds: int = _int("TARGET_SHORT_SECONDS", 35)
    # Kiesmodus: compilations (3 funny moments in one Short) added to every daily batch.
    compilations_per_day: int = _int("COMPILATIONS_PER_DAY", 1)
    compilation_part_seconds: int = _int("COMPILATION_PART_SECONDS", 18)
    font_path: str = os.getenv("FONT_PATH", "").strip()

    @property
    def uploads_per_day(self) -> int:
        return len(self.publish_times)

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
