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
from .twitch import Clip

log = logging.getLogger("shortsbot")
RETRY_AFTER = timedelta(minutes=30)


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


class ApprovalView(discord.ui.View):
    def __init__(self, bot: "ShortsBot", clip: Clip, video, slot: datetime | None):
        super().__init__(timeout=None)
        self.bot, self.clip, self.video, self.slot = bot, clip, video, slot

    @discord.ui.button(label="Uploaden", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(content=f"{interaction.message.content}\n⏳ Uploaden...", view=None)
        await self.bot.upload(self.clip, self.video, self.slot)

    @discord.ui.button(label="Overslaan", style=discord.ButtonStyle.danger, emoji="❌")
    async def reject(self, interaction: discord.Interaction, _button: discord.ui.Button):
        db.mark(self.clip, "rejected")
        self.video.unlink(missing_ok=True)
        await interaction.response.edit_message(content=f"{interaction.message.content}\n❌ Overgeslagen", view=None)


class ShortsBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.lock = asyncio.Lock()
        self.channel: discord.abc.Messageable | None = None
        self.background: asyncio.Task | None = None
        self.retry_at = datetime.min.replace(tzinfo=timezone.utc)
        register_commands(self)

    async def setup_hook(self):
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
        open_slots = pipeline.open_slots()
        todo = f" Ik maak nu {len(open_slots)} shorts voor de komende 24 uur." if open_slots else ""
        await self.say(
            f"🤖 Shorts-bot online. Shorts komen online om {', '.join(config.publish_times)}, "
            f"{'met goedkeuring' if config.approval_mode else 'volledig automatisch'}.{todo} Typ `/status` voor info."
        )

    async def say(self, text: str, **kwargs):
        if self.channel:
            await self.channel.send(text, **kwargs)

    @tasks.loop(minutes=5)
    async def scheduler(self):
        """Fill every publish time in the coming 24 hours, so Shorts go online even when the PC is off."""
        if db.get_setting("paused") == "1" or self.lock.locked():
            return
        if datetime.now(timezone.utc) < self.retry_at:
            return
        for slot in pipeline.open_slots():
            if db.get_setting("paused") == "1":
                return
            if not await self.run_cycle(slot):
                self.retry_at = datetime.now(timezone.utc) + RETRY_AFTER
                return

    @scheduler.before_loop
    async def _wait_ready(self):
        await self.wait_until_ready()

    async def run_cycle(self, slot: datetime | None = None) -> bool:
        """Make one Short. Without a slot it goes online right away. Returns False when it failed."""
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

            if config.approval_mode:
                db.mark(clip, "pending", publish_at=slot.isoformat() if slot else "")
                when = f" Komt online op {_local_time(slot)}." if slot else ""
                await self.say(
                    f"👀 Short klaar: **{clip.title}** ({clip.broadcaster_name}).{when} Uploaden?\n{clip.url}",
                    view=ApprovalView(self, clip, video, slot),
                )
                return True

            return await self._publish(clip, video, slot)

    async def upload(self, clip: Clip, video, slot: datetime | None = None):
        async with self.lock:
            await self._publish(clip, video, slot)

    async def _publish(self, clip: Clip, video, slot: datetime | None) -> bool:
        try:
            video_id = await asyncio.to_thread(pipeline.publish, clip, video, slot)
        except Exception as exc:
            log.exception("Upload mislukt")
            await self.say(f"⚠️ Upload mislukt: {_error_text(exc)}")
            return False
        link = f"https://youtube.com/shorts/{video_id}"
        if slot and slot > datetime.now(timezone.utc) + timedelta(minutes=15):
            await self.say(f"📅 Geüpload! Komt online op {_local_time(slot)}: {link}")
        else:
            await self.say(f"✅ Geüpload! {link}")
        return True


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
        tz = ZoneInfo(config.timezone)
        await interaction.response.send_message(
            f"**Status:** {'⏸️ gepauzeerd' if paused else '▶️ actief'}\n"
            f"**Online-tijden:** {', '.join(config.publish_times)} ({tz.key})\n"
            f"**Modus:** {'goedkeuring nodig' if config.approval_mode else 'volledig automatisch'}\n"
            f"**Ingepland:**\n{planned or 'niks'}\n"
            f"**Laatste uploads:**\n{recent or 'nog geen'}"
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
