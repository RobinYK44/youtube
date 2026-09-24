"""Discord bot: runs the pipeline on a schedule and lets you control it with slash commands."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

from . import db, pipeline
from .config import config
from .youtube import make_hashtags, tiktok_caption
from .twitch import Clip

log = logging.getLogger("shortsbot")
RETRY_AFTER = timedelta(minutes=30)
BATCH_EVERY = timedelta(hours=20)  # kiesmodus: at most one new batch of candidates per day
AUTO_PICK_BEFORE = timedelta(minutes=45)  # kiesmodus: pick the best one yourself if the owner did not
CANDIDATE_MAX_AGE = timedelta(hours=48)
TIKTOK_GAP = timedelta(minutes=45)  # never post to TikTok more often than this, also after catching up
PICK_AHEAD_HOURS = 48  # kiesmodus: picked Shorts may fill publish times up to two days ahead


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
        if ok:
            await interaction.edit_original_response(content=f"{interaction.message.content}\n{text}", view=None)
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
        if ok:
            await interaction.edit_original_response(content=f"{interaction.message.content}\n{text}", view=None)
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
        self.batch_task: asyncio.Task | None = None
        register_commands(self)

    @property
    def making_batch(self) -> bool:
        return self.batch_task is not None and not self.batch_task.done()

    def start_batch(self, coro) -> None:
        """Make candidates in the background, so auto-picking keeps running meanwhile."""
        self.batch_task = asyncio.create_task(coro)

    async def setup_hook(self):
        self.add_dynamic_items(PickButton, RejectButton)
        self.scheduler.start()

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
            f"🤖 Shorts-bot online, {_mode_text()}. Shorts komen online om {', '.join(pipeline.publish_times())}. "
            "Typ `/status` voor info."
        )

    async def say(self, text: str, **kwargs):
        if self.channel:
            await self.channel.send(text, **kwargs)

    # ---- schedule -------------------------------------------------------------------------------------

    @tasks.loop(minutes=5)
    async def scheduler(self):
        """Fill every publish time in the coming 24 hours, so Shorts go online even when the PC is off."""
        now = datetime.now(timezone.utc)
        for clip_id in db.expire_candidates((now - CANDIDATE_MAX_AGE).isoformat()):
            pipeline.video_path(clip_id).unlink(missing_ok=True)
        if db.get_setting("paused") == "1":
            return
        await self.post_tiktok_due()
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
        row = due[0]
        db.set_setting("tiktok_last", now.isoformat())
        async with self.upload_lock:
            try:
                _, public = await asyncio.to_thread(pipeline.post_tiktok, row)
            except Exception as exc:
                log.exception("TikTok mislukt")
                await self.say(f"⚠️ TikTok-upload van **{row['title']}** mislukt: {_error_text(exc)}")
                return
        if public:
            await self.say(f"🎵 Op TikTok gezet: **{row['title']}**")
        else:
            await self.say(
                f"🎵 Op TikTok gezet: **{row['title']}** (🔒 alleen zichtbaar voor jou tot TikTok je app goedkeurt)"
            )

    @scheduler.before_loop
    async def _wait_ready(self):
        await self.wait_until_ready()

    # ---- fully automatic --------------------------------------------------------------------------------

    async def run_cycle(self, slot: datetime | None = None) -> bool:
        """Make one Short and upload it. Without a slot it goes online right away. False when it failed."""
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

        async with self.upload_lock:
            ok, text = await self._publish(clip, video, slot)
        await self.say(text)
        return ok

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
        last = db.get_setting("batch_at")
        now = datetime.now(timezone.utc)
        if last and now - datetime.fromisoformat(last) < BATCH_EVERY:
            return
        open_slots = pipeline.open_slots(hours=PICK_AHEAD_HOURS)
        if not open_slots:
            return
        header = (
            "🎞️ Ik maak **{n} shorts**. "
            f"Kies er maximaal **{len(open_slots)}** uit met **✅ Kies deze**. "
            f"Ze komen online om {', '.join(pipeline.publish_times())}, in de volgorde waarin je ze kiest. "
            "Kies je niet op tijd, dan kies ik 45 minuten van tevoren zelf de beste."
        )
        if await self.make_batch(pipeline.candidates_per_day(), header):
            db.set_setting("batch_at", now.isoformat())
            if config.compilations_per_day:
                await self.make_compilations(config.compilations_per_day)

    async def make_batch(self, count: int, header: str) -> bool:
        """Render `count` new candidates and post them. `header` may contain {n}. False when none were found."""
        try:
            clips = await asyncio.to_thread(pipeline.pick_candidates, count)
        except Exception as exc:
            log.exception("Clips zoeken mislukt")
            await self.say(f"⚠️ Clips zoeken mislukt: {_error_text(exc)}")
            self.retry_at = datetime.now(timezone.utc) + RETRY_AFTER
            return False
        if not clips:
            await self.say("🔍 Geen nieuwe clips gevonden die aan de eisen voldoen. Ik probeer het later opnieuw.")
            self.retry_at = datetime.now(timezone.utc) + RETRY_AFTER
            return False

        await self.say(header.format(n=len(clips)))
        for number, clip in enumerate(clips, 1):
            if db.get_setting("paused") == "1":
                return True
            await self.make_candidate(clip, number, len(clips))
        await self.say(f"👍 Alle {len(clips)} shorts staan klaar. Kies je favorieten!")
        return True

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

    async def make_candidate(self, clip: Clip, number: int, total: int):
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
        view.add_item(PickButton(clip.id))
        view.add_item(RejectButton(clip.id))
        hashtags = " ".join("#" + t for t in make_hashtags(clip))
        if clip.parts:
            lines = "".join(
                f"\n**#{len(clip.parts) - i}** {part.title} — {part.broadcaster_name} ({part.view_count:,} views)"
                for i, part in enumerate(clip.parts)
            )
            text = f"🎞️ **Compilatie {number}/{total}** · **{clip.title}**{lines}\n{hashtags}"
        else:
            text = (
                f"🎬 **{number}/{total}** · **{clip.title}** — {clip.broadcaster_name}"
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
            db.mark(pipeline.clip_from_row(row), "rejected")
            pipeline.video_path(clip_id).unlink(missing_ok=True)
            return True, f"❌ Afgekeurd. Nog {len(db.candidates())} over om uit te kiezen."

    async def pick(self, clip_id: str, auto: bool = False) -> tuple[bool, str]:
        """Upload a candidate into the first free publish time."""
        async with self.upload_lock:
            row = db.get(clip_id)
            if row is None or row["status"] != "candidate":
                return False, "Deze short is al gekozen of verlopen."
            slots = pipeline.open_slots(hours=PICK_AHEAD_HOURS)
            if not slots:
                return False, "Alle tijden voor de komende 2 dagen zijn al gevuld. Morgen kun je weer kiezen."
            clip = pipeline.clip_from_row(row)
            video = pipeline.video_path(clip_id)
            if not video.exists():
                db.mark(clip, "expired")
                return False, "Het videobestand van deze short bestaat niet meer. Kies een andere."
            ok, text = await self._publish(clip, video, slots[0])
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
            f"https://youtube.com/shorts/{r['youtube_id']}"
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
                "Alle tijden voor de komende 2 dagen zijn al gevuld, dus er valt nu niks te kiezen."
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
        )
        bot.retry_at = datetime.min.replace(tzinfo=timezone.utc)
        await interaction.response.send_message(
            f"🗑️ {len(rows)} tijden zijn weer vrij. **Verwijder deze video's zelf in YouTube Studio**, "
            f"anders komen ze alsnog online:\n{links}"
        )

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

    @tree.command(name="nu", description="Maak en upload direct een nieuwe short")
    @admin
    async def now(interaction: discord.Interaction):
        if bot.lock.locked():
            await interaction.response.send_message("⏳ Ik ben al bezig met een short, even geduld.")
            return
        await interaction.response.send_message("🚀 Ik ga meteen een short maken...")
        bot.background = asyncio.create_task(bot.run_cycle())

    @tree.command(name="top", description="De grootste live streamers op dit moment")
    @admin
    async def top(interaction: discord.Interaction):
        await interaction.response.defer()
        streams = await asyncio.to_thread(pipeline.twitch.top_live_streamers, 10, config.discover_language)
        lines = [f"{i}. **{s['name']}** — {s['viewers']:,} kijkers ({s['game']})" for i, s in enumerate(streams, 1)]
        await interaction.followup.send("📈 **Nu live op Twitch:**\n" + "\n".join(lines))

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
        pipeline.add_streamer(naam)
        await interaction.response.send_message(f"➕ **{naam}** toegevoegd.")

    @tree.command(name="streamer_verwijderen", description="Haal een streamer weg")
    @app_commands.describe(naam="Twitch-loginnaam")
    @admin
    async def remove(interaction: discord.Interaction, naam: str):
        pipeline.remove_streamer(naam)
        await interaction.response.send_message(f"➖ **{naam}** verwijderd.")

    @tree.command(name="pauze", description="Stop tijdelijk met uploaden")
    @admin
    async def pause(interaction: discord.Interaction):
        db.set_setting("paused", "1")
        await interaction.response.send_message("⏸️ Gepauzeerd. Gebruik `/hervat` om verder te gaan.")

    @tree.command(name="hervat", description="Ga weer verder met uploaden")
    @admin
    async def resume(interaction: discord.Interaction):
        db.set_setting("paused", "0")
        bot.retry_at = datetime.min.replace(tzinfo=timezone.utc)
        await interaction.response.send_message("▶️ Ik ga weer verder!")
