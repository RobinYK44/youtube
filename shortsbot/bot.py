"""Discord bot: runs the pipeline on a schedule and lets you control it with slash commands."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

from . import db, pipeline
from .config import VERSION, config
from .youtube import NeedsLogin, make_hashtags, tiktok_caption
from .twitch import Clip

log = logging.getLogger("shortsbot")
RETRY_AFTER = timedelta(minutes=30)
BATCH_EVERY = timedelta(hours=20)  # kiesmodus: at most one new batch of candidates per day
AUTO_PICK_BEFORE = timedelta(minutes=45)  # kiesmodus: pick the best one yourself if the owner did not
CANDIDATE_MAX_AGE = timedelta(hours=48)
TIKTOK_GAP = timedelta(minutes=45)  # never post to TikTok more often than this, also after catching up
PICK_AHEAD_HOURS = 7 * 24  # kiesmodus: picked Shorts may fill publish times up to a week ahead
UPLOAD_AHEAD = timedelta(hours=24)  # picked Shorts are uploaded to YouTube this long before their publish time
STATS_EVERY = timedelta(hours=12)
WATCH_MINUTES = 30  # /letop: clips made in the last half hour
WATCH_PER_ROUND = 2  # at most this many new clips every 3 minutes
WATCH_SAME_STREAMER = timedelta(minutes=10)  # not 10 clips of the same moment
MAX_UPLOADS_PER_DAY = 6  # YouTube API quota: 10,000 units a day, an upload costs 1,600


def _local_time(moment: datetime) -> str:
    """Discord timestamp: every viewer sees it in their own time zone."""
    return f"<t:{int(moment.timestamp())}:f>"


def _error_text(exc: Exception) -> str:
    text = str(exc)
    if "quotaExceeded" in text or "uploadLimitExceeded" in text:
        return "YouTube-limiet voor vandaag bereikt. Morgen gaat de bot automatisch verder."
    if "invalid_grant" in text:
        return (
            "YouTube-login is verlopen. Draai op je pc `python -m shortsbot.youtube auth` "
            "en start de bot opnieuw."
        )
    return f"{type(exc).__name__}: {text[:1500]}"


def _mode_text() -> str:
    count = pipeline.candidates_per_day()
    return f"kiesmodus ({count} per dag, jij kiest)" if count else "volledig automatisch"


def _tiktok_only(clip_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(TikTokButton(clip_id))
    return view


class PickButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pick:(?P<clip_id>.+)"):
    """'Kies deze' button under a candidate. Keeps working after the bot restarts."""

    def __init__(self, clip_id: str):
        super().__init__(
            discord.ui.Button(
                label="Kies deze", style=discord.ButtonStyle.success, emoji="✅", custom_id=f"pick:{clip_id}"
            )
        )
        self.clip_id = clip_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["clip_id"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ok, text = await interaction.client.pick(self.clip_id)
        if ok:  # keep only the TikTok button, so a TikTok version can still be asked for afterwards
            await interaction.edit_original_response(
                content=f"{interaction.message.content}\n{text}", view=_tiktok_only(self.clip_id)
            )
        else:
            await interaction.followup.send(text, ephemeral=True)


class RejectButton(discord.ui.DynamicItem[discord.ui.Button], template=r"reject:(?P<clip_id>.+)"):
    """'Afkeuren' button under a candidate: the Short is thrown away and never used."""

    def __init__(self, clip_id: str):
        super().__init__(
            discord.ui.Button(
                label="Afkeuren", style=discord.ButtonStyle.danger, emoji="❌", custom_id=f"reject:{clip_id}"
            )
        )
        self.clip_id = clip_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["clip_id"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ok, text = await interaction.client.reject(self.clip_id)
        if ok:  # keep only the TikTok button, so a TikTok version can still be asked for afterwards
            await interaction.edit_original_response(
                content=f"{interaction.message.content}\n{text}", view=_tiktok_only(self.clip_id)
            )
        else:
            await interaction.followup.send(text, ephemeral=True)


class TikTokButton(discord.ui.DynamicItem[discord.ui.Button], template=r"tiktok:(?P<clip_id>.+)"):
    """'TikTok' button under a candidate: get the TikTok version of this Short, it stays available for YouTube."""

    def __init__(self, clip_id: str):
        super().__init__(
            discord.ui.Button(
                label="TikTok", style=discord.ButtonStyle.secondary, emoji="📱", custom_id=f"tiktok:{clip_id}"
            )
        )
        self.clip_id = clip_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["clip_id"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = await interaction.client.candidate_tiktok(self.clip_id)
        await interaction.followup.send(text, ephemeral=True)


class PostNowButton(discord.ui.DynamicItem[discord.ui.Button], template=r"now:(?P<clip_id>.+)"):
    """'Nu online' button under a clip from /actueel or /letop: upload it and make it public right away."""

    def __init__(self, clip_id: str):
        super().__init__(
            discord.ui.Button(
                label="Nu online", style=discord.ButtonStyle.primary, emoji="🚀", custom_id=f"now:{clip_id}"
            )
        )
        self.clip_id = clip_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["clip_id"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ok, text = await interaction.client.post_now(self.clip_id)
        if ok:
            await interaction.edit_original_response(
                content=f"{interaction.message.content}\n{text}", view=_tiktok_only(self.clip_id)
            )
        else:
            await interaction.followup.send(text, ephemeral=True)


class ShortsBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.lock = asyncio.Lock()  # finding + rendering clips
        self.upload_lock = asyncio.Lock()  # choosing a publish time + uploading
        self.channel: discord.abc.Messageable | None = None
        self.background: asyncio.Task | None = None
        self.retry_at = datetime.min.replace(tzinfo=timezone.utc)
        self.queue_retry_at = datetime.min.replace(tzinfo=timezone.utc)
        self.batch_task: asyncio.Task | None = None
        self.watch_seen: dict[str, datetime] = {}  # /letop: when each streamer last got a new clip sent
        register_commands(self)

    @property
    def making_batch(self) -> bool:
        return self.batch_task is not None and not self.batch_task.done()

    def start_batch(self, coro) -> None:
        """Make candidates in the background, so auto-picking keeps running meanwhile."""
        self.batch_task = asyncio.create_task(coro)

    async def setup_hook(self):
        self.add_dynamic_items(PickButton, RejectButton, TikTokButton, PostNowButton)
        self.scheduler.start()
        self.watcher.start()

    async def on_ready(self):
        if self.channel:  # on_ready fires again after reconnects
            return
        self.channel = self.get_channel(config.discord_channel_id) or await self.fetch_channel(
            config.discord_channel_id
        )
        guild = self.channel.guild
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Ingelogd als %s, kanaal #%s", self.user, self.channel)
        await self.say(
            f"🤖 Shorts-bot online (versie {VERSION}), {_mode_text()}. Shorts komen online om {', '.join(pipeline.publish_times())}. "
            "Typ `/status` voor info."
            + ("\n⏸️ Let op: ik sta nog op **pauze** en zoek geen nieuwe shorts. Typ `/hervat` om verder te gaan."
               if db.get_setting("paused") == "1" else "")
            + ("\n👀 Ik let op nieuwe clips (`/letop aan:False` om te stoppen)." if db.get_setting("watch") == "1" else "")
        )

    async def say(self, text: str, **kwargs):
        if self.channel:
            await self.channel.send(text, **kwargs)

    # ---- schedule -------------------------------------------------------------------------------------

    async def update_stats_if_due(self):
        """Twice a day: read the views of our Shorts, so the bot learns which streamers do well."""
        last = db.get_setting("stats_at")
        now = datetime.now(timezone.utc)
        if last and now - datetime.fromisoformat(last) < STATS_EVERY:
            return
        db.set_setting("stats_at", now.isoformat())
        try:
            await asyncio.to_thread(pipeline.update_view_stats)
        except NeedsLogin as exc:
            if db.get_setting("stats_login_warned") != "1":
                db.set_setting("stats_login_warned", "1")
                await self.say(f"📊 {exc} Daarna leert de bot van welke streamers jouw shorts het best lopen.")
        except Exception:
            log.exception("Views ophalen mislukt")

    def upload_limit_reached(self) -> bool:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        return db.uploads_since(since) >= MAX_UPLOADS_PER_DAY

    async def upload_queued(self):
        """Upload picked Shorts a day before their publish time, at most MAX_UPLOADS_PER_DAY a day."""
        now = datetime.now(timezone.utc)
        if now < self.queue_retry_at:
            return
        for row in db.queued_due((now + UPLOAD_AHEAD).isoformat()):
            if self.upload_limit_reached():
                return
            async with self.upload_lock:
                row = db.get(row["id"])
                if row is None or row["status"] != "queued":
                    continue
                clip = pipeline.clip_from_row(row)
                video = pipeline.video_path(clip.id)
                if not video.exists():
                    db.mark(clip, "failed")
                    await self.say(f"⚠️ Het videobestand van **{clip.title}** is weg. Die tijd is weer vrij.")
                    continue
                slot = datetime.fromisoformat(row["publish_at"])
                if slot < now + timedelta(minutes=15):  # the PC was off at its time: move it to the next free time
                    db.mark(clip, "candidate")  # frees its old time while looking for a new one
                    free = pipeline.open_slots(hours=PICK_AHEAD_HOURS)
                    if not free:
                        await self.say(f"⚠️ Geen vrije tijd meer voor **{clip.title}**. Hij staat weer bij de keuzes.")
                        continue
                    db.mark(clip, "queued", publish_at=free[0].isoformat())
                    await self.say(f"⏰ **{clip.title}** is verschoven naar {_local_time(free[0])} (je laptop stond uit).")
                    continue
                ok, text = await self._publish(clip, video, slot)
                if not ok:
                    db.mark(clip, "queued", publish_at=row["publish_at"])  # keep it, try again later
                    self.queue_retry_at = now + RETRY_AFTER
                    await self.say(f"{text}\nIk probeer **{clip.title}** later opnieuw.")
                    return
            await self.say(text)

    @tasks.loop(minutes=5)
    async def scheduler(self):
        """Fill every publish time in the coming 24 hours, so Shorts go online even when the PC is off."""
        now = datetime.now(timezone.utc)
        db.expire_candidates((now - CANDIDATE_MAX_AGE).isoformat())
        pipeline.cleanup_videos()  # the files are kept a few days, for the TikTok button
        # Shorts the owner already chose still go online during a pause; only finding and making new ones stops.
        await self.post_tiktok_due()
        await self.upload_queued()
        await self.update_stats_if_due()
        if db.get_setting("paused") == "1":
            return
        if now < self.retry_at:
            return
        if pipeline.candidates_per_day():
            if not self.making_batch and not self.lock.locked():
                self.start_batch(self.make_batch_if_due())
            await self.auto_pick_due()
            return
        if self.lock.locked():
            return
        for slot in pipeline.open_slots():
            if db.get_setting("paused") == "1" or pipeline.candidates_per_day():
                return  # paused, or switched to kiesmodus halfway
            if not await self.run_cycle(slot):
                self.retry_at = datetime.now(timezone.utc) + RETRY_AFTER
                return

    async def post_tiktok_due(self):
        """TikTok cannot schedule posts, so the bot posts each Short at its publish time (or later when the
        PC was off), at most one every TIKTOK_GAP."""
        if not pipeline.tiktok_enabled():
            return
        now = datetime.now(timezone.utc)
        last = db.get_setting("tiktok_last")
        if last and now - datetime.fromisoformat(last) < TIKTOK_GAP:
            return
        due = db.tiktok_due(now.isoformat())
        if not due:
            return
        await self.post_tiktok_row(due[0])

    async def post_tiktok_row(self, row):
        db.set_setting("tiktok_last", datetime.now(timezone.utc).isoformat())
        async with self.upload_lock:
            try:
                _, status = await asyncio.to_thread(pipeline.post_tiktok, row)
            except Exception as exc:
                if pipeline.tiktok_needs_audit(exc):
                    await self.send_tiktok_by_hand(row)
                    return
                log.exception("TikTok mislukt")
                await self.say(f"⚠️ TikTok-upload van **{row['title']}** mislukt: {_error_text(exc)}")
                return
        if status == "draft":
            await self.say(
                f"📥 **{row['title']}** staat klaar in je **TikTok-app** (kijk bij je meldingen of inbox). "
                "Open hem, voeg eventueel een trending geluidje toe, plak de tekst hieronder en post hem 👇"
            )
            await self.say(tiktok_caption(pipeline.clip_from_row(row)))
        elif status == "posted":
            await self.say(f"🎵 Op TikTok gezet: **{row['title']}**")
        else:
            await self.say(
                f"🎵 Op TikTok gezet: **{row['title']}** (🔒 alleen zichtbaar voor jou tot TikTok je app goedkeurt)"
            )

    async def send_tiktok_by_hand(self, row):
        """The TikTok app is not approved yet and the account is public: send the video to post by hand."""
        clip = pipeline.clip_from_row(row)
        video = pipeline.video_path(clip.id)
        copy = await asyncio.to_thread(pipeline.tiktok_version, video) if video.exists() else None
        video.unlink(missing_ok=True)
        await self.say(
            "ℹ️ TikTok nam de video niet aan. Dubbelklik een keer op `tiktok_login` op je pc, dan komt hij "
            "voortaan als concept in je TikTok-app. Deze krijg je hier om zelf te posten."
        )
        if copy:
            await self.send_tiktok_copy(clip, copy, None)

    @scheduler.before_loop
    async def _wait_ready(self):
        await self.wait_until_ready()

    # ---- fully automatic --------------------------------------------------------------------------------

    async def run_cycle(self, slot: datetime | None = None, tiktok_only: bool = False) -> bool:
        """Make one Short and upload it. Without a slot it goes online right away. False when it failed.
        With tiktok_only it skips YouTube and only sends the TikTok version to Discord."""
        async with self.lock:
            try:
                clip = await asyncio.to_thread(pipeline.pick_clip)
            except Exception as exc:
                log.exception("Clips zoeken mislukt")
                await self.say(f"⚠️ Clips zoeken mislukt: {_error_text(exc)}")
                return False
            if clip is None:
                await self.say("🔍 Geen nieuwe clips gevonden die aan de eisen voldoen. Ik probeer het later opnieuw.")
                return False

            await self.say(
                f"🎬 Bezig met **{clip.title}** van **{clip.broadcaster_name}** "
                f"({clip.view_count:,} views)\n<{clip.url}>"
            )
            try:
                video = await asyncio.to_thread(pipeline.render, clip)
            except Exception as exc:
                log.exception("Renderen mislukt")
                db.mark(clip, "failed")
                await self.say(f"⚠️ Bewerken mislukt: {_error_text(exc)}")
                return False

        if tiktok_only:
            return await self.send_tiktok_only(clip, video)
        async with self.upload_lock:
            ok, text = await self._publish(clip, video, slot)
        await self.say(text)
        return ok

    async def candidate_tiktok(self, clip_id: str) -> str:
        """TikTok version of a candidate. The candidate itself can still be picked for YouTube."""
        row = db.get(clip_id)
        video = pipeline.video_path(clip_id)
        if row is None or not video.exists():
            return (
                f"Het bestand van deze short is al opgeruimd (ik bewaar ze {pipeline.KEEP_VIDEOS_DAYS} dagen). "
                "Gebruik `/tiktok_clip` met de link van de clip."
            )
        copy = await asyncio.to_thread(pipeline.tiktok_version, video)
        if copy is None:
            return "⚠️ TikTok-versie maken mislukt."
        await self.send_tiktok_copy(pipeline.clip_from_row(row), copy, None)
        return "📱 Staat hieronder in het kanaal!"

    async def tiktok_from_link(self, url: str) -> None:
        """/tiktok_clip: one specific Twitch clip or YouTube video, made into a TikTok video."""
        async with self.lock:
            try:
                clip = await asyncio.to_thread(pipeline.clip_from_link, url)
                await self.say(f"📱 Bezig met **{clip.title}** van **{clip.broadcaster_name}** voor TikTok...")
                video = await asyncio.to_thread(pipeline.render, clip)
            except Exception as exc:
                log.exception("TikTok-video van link mislukt")
                await self.say(f"⚠️ Kon deze video niet maken: {_error_text(exc)}")
                return
        await self.send_tiktok_only(clip, video)

    async def send_tiktok_only(self, clip: Clip, video) -> bool:
        if pipeline.tiktok_enabled():  # automatic TikTok: post it right away
            db.mark(clip, "tiktok")  # used, so it will not show up again for YouTube
            db.set_tiktok(clip.id, "queued")
            await self.post_tiktok_row(db.get(clip.id))
            return db.get(clip.id)["tiktok_status"] != "failed"
        copy = await asyncio.to_thread(pipeline.tiktok_version, video)
        video.unlink(missing_ok=True)
        if copy is None:
            db.mark(clip, "failed")
            await self.say("⚠️ TikTok-versie maken mislukt.")
            return False
        db.mark(clip, "tiktok")  # used, so it will not show up again for YouTube
        await self.send_tiktok_copy(clip, copy, None)
        return True

    async def _publish(self, clip: Clip, video, slot: datetime | None) -> tuple[bool, str]:
        manual_tiktok = not pipeline.tiktok_enabled() and db.get_setting("tiktok_paused") != "1"
        tiktok_copy = await asyncio.to_thread(pipeline.tiktok_version, video) if manual_tiktok else None
        try:
            video_id = await asyncio.to_thread(pipeline.publish, clip, video, slot)
        except Exception as exc:
            log.exception("Upload mislukt")
            if tiktok_copy:
                tiktok_copy.unlink(missing_ok=True)
            return False, f"⚠️ Upload mislukt: {_error_text(exc)}"
        if tiktok_copy:
            await self.send_tiktok_copy(clip, tiktok_copy, slot)
        link = f"https://youtube.com/shorts/{video_id}"
        if clip.source == "vyro":
            await self.say(
                f"💰 **Vyro:** dien deze link in bij de campagne van {clip.broadcaster_name}: {link}\n"
                "Zet je hem ook op TikTok? Dien die link dan ook in."
            )
        if slot and slot > datetime.now(timezone.utc) + timedelta(minutes=15):
            return True, f"📅 Geüpload! Komt online op {_local_time(slot)}: {link}"
        return True, f"✅ Geüpload! {link}"

    async def send_tiktok_copy(self, clip: Clip, video, slot: datetime | None):
        """The video for TikTok plus its text in a separate message, so it is easy to copy on a phone."""
        when = f"rond {_local_time(slot)}" if slot and slot > datetime.now(timezone.utc) else "wanneer je wilt"
        try:
            await self.say(
                f"📱 **TikTok-versie** van **{clip.title}**. Sla de video op en post hem {when}. "
                "Tip: voeg in TikTok een trending geluidje toe (zacht). De tekst om te kopiëren staat hieronder 👇",
                file=discord.File(video),
            )
            await self.say(tiktok_caption(clip))
        except Exception:
            log.exception("TikTok-versie sturen mislukt")
        finally:
            video.unlink(missing_ok=True)

    # ---- kiesmodus --------------------------------------------------------------------------------------

    async def make_batch_if_due(self):
        """Once a day: make the day's candidates. If the bot was closed halfway, only the rest is made later."""
        last = db.get_setting("batch_at")
        now = datetime.now(timezone.utc)
        if last and now - datetime.fromisoformat(last) < BATCH_EVERY:
            left = int(db.get_setting("batch_left") or 0)
            if left > 0 and pipeline.open_slots(hours=PICK_AHEAD_HOURS):
                await self.make_batch(left, "🎞️ Ik maak de laatste **{n} shorts** van vandaag af.", daily="resume")
            return
        open_slots = pipeline.open_slots(hours=PICK_AHEAD_HOURS)
        if not open_slots:
            return
        header = (
            "🎞️ Ik maak **{n} shorts**. "
            f"Kies er zoveel als je wilt met **✅ Kies deze** (nog **{len(open_slots)}** plekken in de komende 7 dagen). "
            f"Ze komen online om {', '.join(pipeline.publish_times())}, in de volgorde waarin je ze kiest. "
            "Kies je niet op tijd, dan kies ik 45 minuten van tevoren zelf de beste."
        )
        if await self.make_batch(pipeline.candidates_per_day(), header, daily="new"):
            if config.compilations_per_day:
                await self.make_compilations(config.compilations_per_day)

    async def make_batch(self, count: int, header: str, daily: str = "") -> bool:
        """Render `count` new candidates and post them. `header` may contain {n}. False when none were found.
        daily='new' starts the day's batch, 'resume' finishes one that was cut off by closing the bot."""
        try:
            clips = await asyncio.to_thread(pipeline.pick_mixed, count)
        except Exception as exc:
            log.exception("Clips zoeken mislukt")
            await self.say(f"⚠️ Clips zoeken mislukt: {_error_text(exc)}")
            self.retry_at = datetime.now(timezone.utc) + RETRY_AFTER
            return False
        if not clips:
            await self.say("🔍 Geen nieuwe clips gevonden die aan de eisen voldoen. Ik probeer het later opnieuw.")
            self.retry_at = datetime.now(timezone.utc) + RETRY_AFTER
            if daily == "resume":
                db.set_setting("batch_left", "0")  # nothing left to find today
            return False
        if daily == "new":
            db.set_setting("batch_at", datetime.now(timezone.utc).isoformat())
        if daily:
            db.set_setting("batch_left", str(len(clips)))  # remembered, in case the bot is closed halfway

        await self.say(header.format(n=len(clips)))
        if pipeline.youtube_channels() and not any(c.source != "twitch" for c in clips):
            await self.say(f"▶️ Geen YouTube-momenten gevonden deze keer ({pipeline.youtube_report_text()}).")
        for number, clip in enumerate(clips, 1):
            if db.get_setting("paused") == "1":
                return True
            await self.make_candidate(clip, number, len(clips))
            if daily:
                db.set_setting("batch_left", str(len(clips) - number))
        await self.say(f"👍 Alle {len(clips)} shorts staan klaar. Kies je favorieten!")
        return True

    async def clip_video(self, url: str, count: int, vyro: bool, hashtags: str) -> None:
        """/knip: moments from one YouTube video as candidates to pick from."""
        try:
            clips, title = await asyncio.to_thread(pipeline.clip_video, url, count, vyro, hashtags)
        except Exception as exc:
            log.exception("YouTube-video ophalen mislukt")
            await self.say(f"⚠️ Kon deze video niet ophalen: {_error_text(exc)}")
            return
        if not clips:
            await self.say(
                f"🔍 Bij **{title}** zie ik (nog) geen 'meest herbekeken'-grafiek, of alle goede momenten zijn "
                "al gebruikt. Die grafiek komt pas als een video genoeg views heeft; probeer het later nog eens."
            )
            return
        tags = " ".join("#" + t for t in clips[0].tags)
        extra = f" in Vyro-stijl ({tags}, geen eigen logo's)" if vyro else ""
        await self.say(f"✂️ Ik knip **{len(clips)} momenten** uit **{title}**{extra}. Kies met **✅**.")
        for number, clip in enumerate(clips, 1):
            if db.get_setting("paused") == "1":
                return
            await self.make_candidate(clip, number, len(clips))

    async def make_compilations(self, count: int) -> None:
        """Compilations of 3 funny moments, posted as candidates to pick from."""
        for number in range(1, count + 1):
            try:
                parts = await asyncio.to_thread(pipeline.pick_compilation)
            except Exception as exc:
                log.exception("Clips zoeken mislukt")
                await self.say(f"⚠️ Clips zoeken mislukt: {_error_text(exc)}")
                return
            if not parts:
                await self.say("🔍 Niet genoeg korte, grappige clips over voor een compilatie. Probeer het later nog eens.")
                return
            await self.make_candidate(pipeline.compilation_clip(parts), number, count)

    @tasks.loop(minutes=3)
    async def watcher(self):
        """/letop: every few minutes, new clips of the favourite streamers from the last minutes."""
        if db.get_setting("watch") != "1" or db.get_setting("paused") == "1":
            return
        if self.lock.locked() or self.making_batch:
            return  # busy making Shorts; look again next round
        now = datetime.now(timezone.utc)
        skip = {login for login, seen in self.watch_seen.items() if now - seen < WATCH_SAME_STREAMER}
        minutes = int(db.get_setting("watch_minutes") or WATCH_MINUTES)
        try:
            clips = await asyncio.to_thread(pipeline.new_clips, minutes, skip)
        except Exception:
            log.exception("Nieuwe clips zoeken mislukt")
            return
        for clip in clips[:WATCH_PER_ROUND]:
            self.watch_seen[clip.broadcaster_login] = now
            await self.make_candidate(clip, 1, 1, fresh="new")

    @watcher.before_loop
    async def _watcher_wait_ready(self):
        await self.wait_until_ready()

    async def fresh(self, hours: float) -> None:
        """/actueel: clips that are blowing up right now, or nothing at all."""
        try:
            clips = await asyncio.to_thread(pipeline.fresh_clips, hours)
        except Exception as exc:
            log.exception("Verse clips zoeken mislukt")
            await self.say(f"⚠️ Zoeken mislukt: {_error_text(exc)}")
            return
        if not clips:
            await self.say(
                f"🔍 Nu niks dat echt ontploft (van de laatste {hours:g} uur). Probeer het later nog eens."
            )
            return
        await self.say(
            f"🔥 **{len(clips)} verse clip(s)** die nu ontploffen! Met **🚀 Nu online** staat hij meteen op YouTube, "
            "zodat je er als een van de eersten bij bent."
        )
        for number, clip in enumerate(clips, 1):
            await self.make_candidate(clip, number, len(clips), fresh="hot")

    async def post_now(self, clip_id: str) -> tuple[bool, str]:
        """Upload a candidate and make it public right away, without waiting for a publish time."""
        async with self.upload_lock:
            row = db.get(clip_id)
            if row is None or row["status"] != "candidate":
                return False, "Deze short is al gekozen of verlopen."
            if self.upload_limit_reached():
                return False, "YouTube-limiet voor vandaag bereikt (6 uploads). Kies hem met ✅ voor een latere tijd."
            clip = pipeline.clip_from_row(row)
            video = pipeline.video_path(clip_id)
            if not video.exists():
                db.mark(clip, "expired")
                return False, "Het videobestand van deze short bestaat niet meer."
            return await self._publish(clip, video, None)

    async def make_candidate(self, clip: Clip, number: int, total: int, fresh: str = ""):
        """fresh: 'hot' (/actueel) or 'new' (/letop) adds the Post-now button."""
        async with self.lock:
            try:
                video = await asyncio.to_thread(pipeline.render, clip)
            except Exception as exc:
                log.exception("Renderen mislukt")
                db.mark(clip, "failed")
                await self.say(f"⚠️ Bewerken van {clip.title} mislukt: {_error_text(exc)}")
                return
            db.mark(clip, "candidate")
            for part in clip.parts:
                db.mark(part, "in_compilation")  # never used again on its own
            preview = await asyncio.to_thread(pipeline.preview, video)
        view = discord.ui.View(timeout=None)
        if fresh:
            view.add_item(PostNowButton(clip.id))
        view.add_item(PickButton(clip.id))
        view.add_item(RejectButton(clip.id))
        view.add_item(TikTokButton(clip.id))
        hashtags = " ".join("#" + t for t in make_hashtags(clip))
        if clip.parts:
            lines = "".join(
                f"\n**#{len(clip.parts) - i}** {part.title} — {part.broadcaster_name} ({part.view_count:,} views)"
                for i, part in enumerate(clip.parts)
            )
            text = f"🎞️ **Compilatie {number}/{total}** · **{clip.title}**{lines}\n{hashtags}"
        elif fresh == "new":
            text = (
                f"🆕 **Net gebeurd** · **{clip.title}** — {clip.broadcaster_name}"
                f"\n⏱️ {pipeline.minutes_old(clip):.0f} min geleden · {clip.view_count:,} views"
                + (f" · {clip.score:.0f} clips van dit moment" if clip.score > 1 else "")
                + f"\n{hashtags}\n<{clip.url}>"
            )
        elif clip.source != "twitch":
            label = "💰 **Vyro** · " if clip.source == "vyro" else "▶️ **YouTube** · "
            minute, second = divmod(int(clip.start), 60)
            text = (
                f"{label}**{number}/{total}** · **{clip.title}** — {clip.broadcaster_name}"
                f"\n🔥 meest herbekeken moment op {minute}:{second:02d} (video: {clip.view_count:,} views)"
                f"\n{hashtags}\n<{clip.url}>"
            )
        else:
            text = (
                f"🟣 **Twitch** · **{number}/{total}** · **{clip.title}** — {clip.broadcaster_name}"
                f"\n🔥 {clip.view_count:,} views in {pipeline.age_hours(clip):.0f} uur\n{hashtags}\n<{clip.url}>"
            )
        try:
            await self.say(text, file=discord.File(preview) if preview else None, view=view)
        finally:
            if preview:
                preview.unlink(missing_ok=True)

    async def reject(self, clip_id: str) -> tuple[bool, str]:
        """Throw a candidate away. It will not be picked, also not automatically."""
        async with self.upload_lock:
            row = db.get(clip_id)
            if row is None or row["status"] != "candidate":
                return False, "Deze short is al gekozen of verlopen."
            db.mark(pipeline.clip_from_row(row), "rejected")  # the file stays a few days, for the TikTok button
            return True, f"❌ Afgekeurd. Nog {len(db.candidates())} over om uit te kiezen."

    async def pick(self, clip_id: str, auto: bool = False) -> tuple[bool, str]:
        """Upload a candidate into the first free publish time."""
        async with self.upload_lock:
            row = db.get(clip_id)
            if row is None or row["status"] != "candidate":
                return False, "Deze short is al gekozen of verlopen."
            slots = pipeline.open_slots(hours=PICK_AHEAD_HOURS)
            if not slots:
                return False, "Alle tijden voor de komende 7 dagen zijn al gevuld. Morgen kun je weer kiezen."
            clip = pipeline.clip_from_row(row)
            video = pipeline.video_path(clip_id)
            if not video.exists():
                db.mark(clip, "expired")
                return False, "Het videobestand van deze short bestaat niet meer. Kies een andere."
            slot = slots[0]
            if slot - datetime.now(timezone.utc) > UPLOAD_AHEAD or self.upload_limit_reached():
                # Uploading everything now would hit YouTube's daily limit: upload it the day before instead.
                db.mark(clip, "queued", publish_at=slot.isoformat())
                ok, text = True, (
                    f"🗓️ Gekozen voor {_local_time(slot)}. Ik zet hem een dag van tevoren op YouTube "
                    "(zorg dat je laptop dan even aan staat)."
                )
            else:
                ok, text = await self._publish(clip, video, slot)
            if ok and not auto:
                left = len(slots) - 1
                text += f"\nNog {left} te kiezen." if left else "\nAlle tijden zijn gevuld! 🎉"
            return ok, text

    async def auto_pick_due(self):
        """Owner did not pick in time: use the most-viewed candidate for publish times that are close."""
        now = datetime.now(timezone.utc)
        for slot in pipeline.open_slots():
            if slot - now > AUTO_PICK_BEFORE:
                return
            candidates = db.candidates()
            if not candidates:
                return
            best = candidates[0]
            ok, text = await self.pick(best["id"], auto=True)
            await self.say(f"⏰ Je had nog niks gekozen voor {_local_time(slot)}, dus ik koos **{best['title']}**.\n{text}")
            if not ok:
                return


def register_commands(bot: ShortsBot):
    tree = bot.tree
    admin = app_commands.default_permissions(manage_guild=True)

    @tree.command(name="status", description="Laat zien wat de bot doet")
    @admin
    async def status(interaction: discord.Interaction):
        paused = db.get_setting("paused") == "1"
        now = datetime.now(timezone.utc)
        planned = "\n".join(
            f"• {_local_time(datetime.fromisoformat(r['publish_at']))} — {r['broadcaster']}: "
            + (f"https://youtube.com/shorts/{r['youtube_id']}" if r["youtube_id"] else "gekozen, nog niet geüpload")
            for r in db.scheduled_after(now.isoformat())
        )
        recent = "\n".join(
            f"• {r['broadcaster']}: https://youtube.com/shorts/{r['youtube_id']}" for r in db.recent_uploads(5)
        )
        waiting = f"**Klaar om te kiezen:** {len(db.candidates())}\n" if pipeline.candidates_per_day() else ""
        if pipeline.tiktok_enabled():
            waiting += f"**TikTok:** automatisch, {db.tiktok_queue_size()} in de wachtrij\n"
        elif db.get_setting("tiktok_paused") != "1":
            waiting += "**TikTok:** je krijgt na elke upload de TikTok-versie in Discord\n"
        await interaction.response.send_message(
            f"**Status:** {'⏸️ gepauzeerd' if paused else '▶️ actief'}\n"
            f"**Modus:** {_mode_text()}\n"
            f"**Online-tijden:** {', '.join(pipeline.publish_times())} ({ZoneInfo(config.timezone).key})\n"
            f"{waiting}"
            f"**Ingepland:**\n{planned or 'niks'}\n"
            f"**Laatste uploads:**\n{recent or 'nog geen'}"
        )

    @tree.command(name="kiesmodus", description="Laat de bot elke dag meerdere shorts maken waar jij uit kiest")
    @app_commands.describe(aantal="Hoeveel shorts per dag maken (0 = volledig automatisch)")
    @admin
    async def choose_mode(interaction: discord.Interaction, aantal: app_commands.Range[int, 0, 30]):
        db.set_setting("candidates_per_day", str(aantal))
        db.set_setting("batch_at", "")
        bot.retry_at = datetime.min.replace(tzinfo=timezone.utc)
        if aantal:
            await interaction.response.send_message(
                f"✅ Kiesmodus aan: ik maak elke dag {aantal} shorts en jij kiest welke online gaan. "
                "Binnen 5 minuten begin ik."
            )
        else:
            await interaction.response.send_message("✅ Kiesmodus uit: ik kies en upload weer helemaal zelf.")

    @tree.command(name="meer", description="Zoek meer shorts om uit te kiezen")
    @app_commands.describe(aantal="Hoeveel extra shorts (standaard 10)")
    @admin
    async def more(interaction: discord.Interaction, aantal: app_commands.Range[int, 1, 30] = 10):
        if bot.making_batch:
            await interaction.response.send_message("⏳ Ik ben nog bezig met shorts maken. Probeer het zo nog eens.")
            return
        if not pipeline.open_slots(hours=PICK_AHEAD_HOURS):
            await interaction.response.send_message(
                "Alle tijden voor de komende 7 dagen zijn al gevuld, dus er valt nu niks te kiezen."
            )
            return
        await interaction.response.send_message(f"🔍 Ik zoek {aantal} nieuwe shorts, even geduld...")
        header = "➕ Nog **{n} shorts** erbij, de beste die er nog zijn. Kies met **✅** of keur af met **❌**."
        bot.start_batch(bot.make_batch(aantal, header))

    @tree.command(name="compilatie", description="Maak een short met 3 grappige momenten van verschillende streamers")
    @app_commands.describe(aantal="Hoeveel compilaties (standaard 1)")
    @admin
    async def compilation(interaction: discord.Interaction, aantal: app_commands.Range[int, 1, 5] = 1):
        if bot.making_batch:
            await interaction.response.send_message("⏳ Ik ben nog bezig met shorts maken. Probeer het zo nog eens.")
            return
        await interaction.response.send_message(
            f"🎞️ Ik maak {aantal} compilatie{'s' if aantal > 1 else ''} met 3 grappige momenten, even geduld..."
        )
        bot.start_batch(bot.make_compilations(aantal))

    @tree.command(name="ingepland_wissen", description="Maak de tijden van ingeplande shorts weer vrij")
    @admin
    async def clear_scheduled(interaction: discord.Interaction):
        rows = db.cancel_scheduled_after(datetime.now(timezone.utc).isoformat())
        for row in rows:
            pipeline.video_path(row["id"]).unlink(missing_ok=True)  # kept for TikTok, not needed anymore
        if not rows:
            await interaction.response.send_message("Er staat niks ingepland.")
            return
        links = "\n".join(
            f"• {_local_time(datetime.fromisoformat(r['publish_at']))} — https://studio.youtube.com/video/{r['youtube_id']}/edit"
            for r in rows
            if r["youtube_id"]
        )
        bot.retry_at = datetime.min.replace(tzinfo=timezone.utc)
        text = f"🗑️ {len(rows)} tijden zijn weer vrij."
        if links:
            text += f" **Verwijder deze video's zelf in YouTube Studio**, anders komen ze alsnog online:\n{links}"
        await interaction.response.send_message(text)

    @tree.command(name="tijden", description="Kies op welke tijden de shorts online komen")
    @app_commands.describe(tijden="Nederlandse tijden, bijv. 18:00, 21:00, 00:00, 02:00 (max 6)")
    @admin
    async def times(interaction: discord.Interaction, tijden: str):
        parsed = pipeline.parse_times(tijden)
        if not parsed:
            await interaction.response.send_message("Dat snap ik niet. Typ het zo: `18:00, 21:00, 00:00, 02:00`")
            return
        if len(parsed) > 6:
            await interaction.response.send_message("Maximaal 6 tijden per dag, anders is de YouTube-limiet op.")
            return
        db.set_setting("publish_times", ",".join(parsed))
        bot.retry_at = datetime.min.replace(tzinfo=timezone.utc)
        await interaction.response.send_message(
            f"🕒 Shorts komen nu online om **{', '.join(parsed)}** (Nederlandse tijd). "
            "Shorts die al ingepland staan houden hun oude tijd."
        )

    @tree.command(name="tiktok", description="TikTok-versies van je shorts aan of uit")
    @app_commands.describe(aan="Aan of uit")
    @admin
    async def tiktok_toggle(interaction: discord.Interaction, aan: bool):
        db.set_setting("tiktok_paused", "0" if aan else "1")
        if not aan:
            await interaction.response.send_message("🎵 TikTok staat uit.")
        elif pipeline.tiktok_enabled():
            await interaction.response.send_message("🎵 TikTok staat aan: ik post de shorts zelf op TikTok.")
        else:
            await interaction.response.send_message(
                "🎵 TikTok staat aan: na elke upload stuur ik je de TikTok-versie en de tekst om te plakken."
            )

    @tree.command(name="letop", description="Blijf letten op nieuwe clips van je streamers (van de laatste minuten)")
    @app_commands.describe(aan="Aan of uit (standaard aan)", minuten="Hoe nieuw de clips moeten zijn (standaard 30)")
    @admin
    async def watch(
        interaction: discord.Interaction, aan: bool = True, minuten: app_commands.Range[int, 5, 120] = WATCH_MINUTES
    ):
        db.set_setting("watch", "1" if aan else "0")
        db.set_setting("watch_minutes", str(minuten))
        if not aan:
            await interaction.response.send_message("👀 Ik let niet meer op nieuwe clips.")
            return
        await interaction.response.send_message(
            f"👀 Ik let nu elke 3 minuten op **nieuwe clips** van je streamers (gemaakt in de laatste {minuten} min). "
            "Zodra er iets gebeurt, stuur ik hem hier met **🚀 Nu online**. Uitzetten: `/letop aan:False`."
        )

    @tree.command(name="actueel", description="Zoek clips die nu net ontploffen, om er als eerste bij te zijn")
    @app_commands.describe(uren="Hoe nieuw (standaard: gemaakt in de laatste 6 uur)")
    @admin
    async def fresh(interaction: discord.Interaction, uren: app_commands.Range[int, 1, 24] = 6):
        if bot.making_batch:
            await interaction.response.send_message(
                "⏳ Ik ben nog bezig met shorts maken. Probeer het als ik klaar ben nog eens."
            )
            return
        await interaction.response.send_message("🔥 Ik kijk wat er nu net ontploft...")
        bot.start_batch(bot.fresh(uren))

    @tree.command(name="nuyoutube", description="Maak direct een nieuwe short en zet hem op YouTube")
    @admin
    async def now_youtube(interaction: discord.Interaction):
        if bot.lock.locked():
            await interaction.response.send_message("⏳ Ik ben al bezig met een short, even geduld.")
            return
        await interaction.response.send_message("🚀 Ik ga meteen een short maken voor YouTube...")
        bot.background = asyncio.create_task(bot.run_cycle())

    @tree.command(name="nutiktok", description="Maak direct een nieuwe short alleen voor TikTok (niet op YouTube)")
    @admin
    async def now_tiktok(interaction: discord.Interaction):
        if bot.lock.locked():
            await interaction.response.send_message("⏳ Ik ben al bezig met een short, even geduld.")
            return
        await interaction.response.send_message("📱 Ik ga meteen een short maken voor TikTok...")
        bot.background = asyncio.create_task(bot.run_cycle(tiktok_only=True))

    @tree.command(name="tiktok_clip", description="Maak van een bepaalde Twitch-clip of YouTube-video een TikTok-video")
    @app_commands.describe(
        link="Link van een Twitch-clip, of een YouTube-video (met &t=90s begint hij op die tijd)"
    )
    @admin
    async def tiktok_clip(interaction: discord.Interaction, link: str):
        if bot.lock.locked():
            await interaction.response.send_message("⏳ Ik ben al bezig met een short, probeer het zo nog eens.")
            return
        await interaction.response.send_message("📱 Ik ga ermee aan de slag...")
        bot.background = asyncio.create_task(bot.tiktok_from_link(link))

    @tree.command(name="top", description="De grootste live streamers op dit moment")
    @admin
    async def top(interaction: discord.Interaction):
        await interaction.response.defer()
        streams = await asyncio.to_thread(pipeline.twitch.top_live_streamers, 10, config.discover_language)
        lines = [f"{i}. **{s['name']}** — {s['viewers']:,} kijkers ({s['game']})" for i, s in enumerate(streams, 1)]
        await interaction.followup.send("📈 **Nu live op Twitch:**\n" + "\n".join(lines))

    @tree.command(name="knip", description="Knip de beste momenten uit een YouTube-video (ook voor Vyro)")
    @app_commands.describe(
        link="Link naar de YouTube-video",
        aantal="Hoeveel momenten (standaard 5)",
        vyro="Voor een Vyro-campagne: alleen de hashtags van de campagne, geen eigen logo's",
        hashtags="Vyro: hashtags van de campagne, bijv. #mrbeast #mrbeastpartner (leeg = automatisch)",
    )
    @admin
    async def cut(
        interaction: discord.Interaction, link: str, aantal: app_commands.Range[int, 1, 10] = 5,
        vyro: bool = False, hashtags: str = "",
    ):
        if bot.making_batch:
            await interaction.response.send_message("⏳ Ik ben nog bezig met shorts maken. Probeer het zo nog eens.")
            return
        await interaction.response.send_message("✂️ Ik zoek de beste momenten, even geduld...")
        bot.start_batch(bot.clip_video(link, aantal, vyro, hashtags))

    @tree.command(name="statistieken", description="Welke shorts en streamers het best lopen op je kanaal")
    @admin
    async def stats(interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            await asyncio.to_thread(pipeline.update_view_stats)
        except NeedsLogin as exc:
            await interaction.followup.send(f"📊 {exc}")
            return
        except Exception as exc:
            await interaction.followup.send(f"⚠️ Views ophalen mislukt: {_error_text(exc)}")
            return
        rows = sorted(pipeline.measured_shorts(), key=lambda r: int(r["yt_views"]), reverse=True)
        if not rows:
            await interaction.followup.send("📊 Nog geen shorts die al 2 dagen online staan. Kijk over een paar dagen weer.")
            return
        top = "\n".join(
            f"{i}. **{int(r['yt_views']):,}** views — {r['title']} ({r['broadcaster']}) "
            f"<https://youtube.com/shorts/{r['youtube_id']}>"
            for i, r in enumerate(rows[:5], 1)
        )
        total = sum(int(r["yt_views"]) for r in rows)
        learned = sorted(pipeline.performance().items(), key=lambda item: item[1], reverse=True)
        if learned:
            good = ", ".join(f"{name} ({factor}×)" for name, factor in learned[:3] if factor > 1)
            bad = ", ".join(f"{name} ({factor}×)" for name, factor in learned[::-1][:3] if factor < 1)
            lesson = f"\n\n🧠 **Doet het goed:** {good or '-'}\n🐢 **Minder:** {bad or '-'}\nDaar kies ik voortaan meer of minder van."
        else:
            lesson = f"\n\n🧠 Vanaf {pipeline.STATS_MIN_SHORTS} shorts die 2 dagen online staan, leer ik welke streamers het best lopen."
        await interaction.followup.send(
            f"📊 **{len(rows)} shorts, samen {total:,} views** (laatste {pipeline.STATS_DAYS} dagen)\n"
            f"**Beste shorts:**\n{top}{lesson}"
        )

    @tree.command(name="youtubers", description="Van welke YouTube-kanalen ik momenten knip")
    @admin
    async def youtubers(interaction: discord.Interaction):
        channels = pipeline.youtube_channels()
        await interaction.response.send_message(
            "▶️ **YouTubers:** " + (", ".join(channels) if channels else "geen (alleen Twitch)")
        )

    @tree.command(name="youtuber_toevoegen", description="Voeg een YouTube-kanaal toe om momenten uit te knippen")
    @app_commands.describe(naam="Naam uit de link, bijv. MrBeast van youtube.com/@MrBeast")
    @admin
    async def add_youtuber(interaction: discord.Interaction, naam: str):
        pipeline.add_youtube_channel(naam.split("@")[-1].split("/")[0])
        await interaction.response.send_message(f"➕ **{naam}** toegevoegd.")

    @tree.command(name="youtuber_verwijderen", description="Haal een YouTube-kanaal weg")
    @app_commands.describe(naam="Naam uit de link, bijv. MrBeast")
    @admin
    async def remove_youtuber(interaction: discord.Interaction, naam: str):
        pipeline.remove_youtube_channel(naam.split("@")[-1].split("/")[0])
        await interaction.response.send_message(f"➖ **{naam}** verwijderd.")

    @tree.command(name="streamers", description="Van welke streamers ik clips zoek")
    @admin
    async def streamers(interaction: discord.Interaction):
        await interaction.response.defer()
        logins = await asyncio.to_thread(pipeline.streamer_list)
        await interaction.followup.send("🎮 **Streamers:** " + ", ".join(logins))

    @tree.command(name="streamer_toevoegen", description="Voeg een Twitch-streamer toe")
    @app_commands.describe(naam="Twitch-loginnaam, bijv. jynxzi")
    @admin
    async def add(interaction: discord.Interaction, naam: str):
        await interaction.response.defer()
        login = naam.strip().lower().removeprefix("https://").removeprefix("www.").removeprefix("twitch.tv/")
        found = await asyncio.to_thread(pipeline.twitch.user_ids, [login])
        if login not in found:
            await interaction.followup.send(
                f"❓ Ik kan **{naam}** niet vinden op Twitch. Gebruik de naam uit de link, "
                "bijv. `jynxzi` van twitch.tv/jynxzi."
            )
            return
        pipeline.add_streamer(login)
        await interaction.followup.send(f"➕ **{found[login][1]}** toegevoegd.")

    @tree.command(name="streamer_verwijderen", description="Haal een streamer weg")
    @app_commands.describe(naam="Twitch-loginnaam")
    @admin
    async def remove(interaction: discord.Interaction, naam: str):
        pipeline.remove_streamer(naam)
        await interaction.response.send_message(f"➖ **{naam}** verwijderd.")

    @tree.command(name="pauze", description="Stop tijdelijk met nieuwe shorts zoeken en maken")
    @admin
    async def pause(interaction: discord.Interaction):
        db.set_setting("paused", "1")
        await interaction.response.send_message(
            "⏸️ Gepauzeerd: ik zoek en maak geen nieuwe shorts meer. Shorts die je al gekozen hebt, zet ik gewoon "
            "nog online. Gebruik `/hervat` om weer verder te gaan."
        )

    @tree.command(name="hervat", description="Ga weer verder met shorts zoeken en maken")
    @admin
    async def resume(interaction: discord.Interaction):
        db.set_setting("paused", "0")
        bot.retry_at = datetime.min.replace(tzinfo=timezone.utc)
        await interaction.response.send_message("▶️ Ik ga weer verder!")
