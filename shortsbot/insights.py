"""Read-only access to the channel's numbers, so Claude can look at them from its own (cloud) sessions.

    python -m shortsbot.insights login    (on the PC, once) a separate login that can only READ
    python -m shortsbot.insights rapport  (anywhere) views, watch time and traffic sources of the last 28 days

The login cannot upload, change or delete anything. Its three values go into the Claude Code cloud environment
as environment variables (YT_READ_CLIENT_ID, YT_READ_CLIENT_SECRET, YT_READ_REFRESH_TOKEN), never into a chat.
Taking the access away again: myaccount.google.com/permissions -> shortsbot -> remove access.
"""
import os
import subprocess
import sys
from datetime import date, timedelta

READ_SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
ENV_NAMES = ("YT_READ_CLIENT_ID", "YT_READ_CLIENT_SECRET", "YT_READ_REFRESH_TOKEN")
TRAFFIC_NAMES = {
    "SHORTS": "Shorts-feed",
    "YT_CHANNEL": "Kanaalpagina",
    "YT_SEARCH": "Zoeken op YouTube",
    "SUBSCRIBER": "Abonnees",
    "EXT_URL": "Extern",
    "NO_LINK_OTHER": "Direct of onbekend",
    "RELATED_VIDEO": "Aanbevolen video's",
    "PLAYLIST": "Playlists",
    "YT_OTHER_PAGE": "Andere YouTube-pagina's",
    "NOTIFICATION": "Meldingen",
}


def login() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    from .config import DATA_DIR, config
    from .youtube import _open_login_page

    secrets = config.youtube_client_secrets
    if not secrets.exists():
        sys.exit(f"Bestand {secrets.name} niet gevonden in {secrets.parent}.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), READ_SCOPES)
    make_url = flow.authorization_url

    def authorization_url(**kwargs):
        url, state = make_url(**kwargs)
        _open_login_page(url)
        return url, state

    flow.authorization_url = authorization_url
    creds = flow.run_local_server(
        port=0, open_browser=False, authorization_prompt_message="", prompt="consent", access_type="offline",
        success_message="Gelukt! Je kunt dit tabblad sluiten en teruggaan naar het zwarte venster.",
    )
    out = DATA_DIR / "claude_toegang.txt"
    out.write_text(
        "Zet deze drie regels in de instellingen van je Claude-omgeving (NIET in een chat plakken):\n"
        "Claude-app -> dit gesprek -> naam van de omgeving bovenaan -> Edit -> Environment variables\n\n"
        f"YT_READ_CLIENT_ID={creds.client_id}\n"
        f"YT_READ_CLIENT_SECRET={creds.client_secret}\n"
        f"YT_READ_REFRESH_TOKEN={creds.refresh_token}\n\n"
        "Deze toegang kan alleen je cijfers LEZEN: niks uploaden, aanpassen of verwijderen.\n"
        "Intrekken: myaccount.google.com/permissions -> shortsbot -> Toegang verwijderen.\n"
        "Verwijder dit bestand als je klaar bent.\n",
        encoding="utf-8",
    )
    print(f"Klaar! Open {out} en volg wat erin staat.")
    if os.name == "nt":
        subprocess.Popen(["notepad", str(out)])


def _services():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    missing = [name for name in ENV_NAMES if not os.getenv(name)]
    if missing:
        sys.exit("Geen leestoegang ingesteld: " + ", ".join(missing) + " ontbreekt.")
    creds = Credentials(
        None,
        refresh_token=os.environ["YT_READ_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_READ_CLIENT_ID"],
        client_secret=os.environ["YT_READ_CLIENT_SECRET"],
        scopes=READ_SCOPES,
    )
    return (
        build("youtube", "v3", credentials=creds, cache_discovery=False),
        build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False),
    )


def report(days: int = 28) -> str:
    youtube, analytics = _services()
    end = date.today()
    start = end - timedelta(days=days)
    query = {"ids": "channel==MINE", "startDate": start.isoformat(), "endDate": end.isoformat()}
    channel = youtube.channels().list(part="snippet,statistics", mine=True).execute()["items"][0]
    stats = channel["statistics"]
    lines = [
        f"Kanaal: {channel['snippet']['title']} — {stats.get('subscriberCount', '?')} abonnees, "
        f"{stats.get('viewCount', '?')} views totaal, {stats.get('videoCount', '?')} video's",
        f"Periode: {start} t/m {end}",
    ]
    try:
        total = analytics.reports().query(
            **query, metrics="views,averageViewDuration,averageViewPercentage,likes,subscribersGained"
        ).execute()
        if total.get("rows"):
            views, duration, percentage, likes, subs = total["rows"][0]
            lines.append(
                f"Totaal: {views} views, gem. {duration:.0f} s gekeken ({percentage:.0f}% van de video), "
                f"{likes} likes, +{subs} abonnees"
            )
    except Exception as exc:
        lines.append(f"(totalen niet gelukt: {exc})")
    try:
        traffic = analytics.reports().query(
            **query, metrics="views", dimensions="insightTrafficSourceType", sort="-views"
        ).execute()
        rows = traffic.get("rows") or []
        all_views = sum(r[1] for r in rows) or 1
        lines.append("\nWaar de views vandaan komen:")
        lines += [f"  {TRAFFIC_NAMES.get(r[0], r[0])}: {r[1]} ({100 * r[1] / all_views:.0f}%)" for r in rows]
    except Exception as exc:
        lines.append(f"(bronnen niet gelukt: {exc})")
    try:
        per_video = analytics.reports().query(
            **query, metrics="views,averageViewDuration,averageViewPercentage,likes",
            dimensions="video", sort="-views", maxResults=50,
        ).execute()
        rows = per_video.get("rows") or []
        titles = {}
        ids = [r[0] for r in rows]
        for i in range(0, len(ids), 50):
            for item in youtube.videos().list(part="snippet", id=",".join(ids[i : i + 50])).execute()["items"]:
                titles[item["id"]] = item["snippet"]["title"]
        lines.append(f"\nPer short (beste eerst, {len(rows)} met views):")
        lines += [
            f"  {views:>5} views | {duration:>3.0f} s | {percentage:>3.0f}% gekeken | {likes} likes | "
            f"{titles.get(video, video)[:70]}"
            for video, views, duration, percentage, likes in rows
        ]
    except Exception as exc:
        lines.append(f"(per short niet gelukt: {exc})")
    return "\n".join(lines)


if __name__ == "__main__":
    if sys.argv[1:] == ["login"]:
        login()
    elif sys.argv[1:2] == ["rapport"]:
        print(report(int(sys.argv[2]) if len(sys.argv) > 2 else 28))
    else:
        sys.exit("Gebruik: python -m shortsbot.insights login | rapport [dagen]")
