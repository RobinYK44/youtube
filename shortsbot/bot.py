"""Discord bot: runs the pipeline on a schedule and lets you control it with slash commands."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import tasks

from . import db, pipeline
from .config import config
from .twitch import Clip

log = logging.getLogger("shortsbot")


def _interval() -> timedelta:
    return timedelta(hours=24 / max(config.uploads_per_day, 1))


def _next_run() -> datetime:
    value = db.get_setting("next_run")
    return datetime.fromisoformat(value) if value else datetime.now(timezone.utc)


def _error_text(exc: Exception) -> str:
    text = str(exc)
    if "quotaExceeded" in text or "uploadLimitExceeded" in text:
        return "YouTube-limiet voor vandaag bereikt. Morgen gaat de bot automatisch verder."
    return f"{type(exc).__name__}: {text[:1500]}"


class ApprovalView(discord.ui.View):
    def __init__(self, bot: "ShortsBot", clip: Clip, video):
        super().__init__(timeout=None)
        self.bot, self.clip, self.video = bot, clip, video

    @discord.ui.button(label="Uploaden", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(content=f"{interaction.message.content}\n⏳ Uploaden...", view=None)
        await self.bot.upload(self.clip, self.video)

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
        await self.say(
            f"🤖 Shorts-bot online. {config.uploads_per_day} shorts per dag, "
            f"{'met goedkeuring' if config.approval_mode else 'volledig automatisch'}. Typ `/status` voor info."
        )

    async def say(self, text: str, **kwargs):
        if self.channel:
            await self.channel.send(text, **kwargs)

    @tasks.loop(minutes=5)
    async def scheduler(self):
        if db.get_setting("paused") == "1" or self.lock.locked():
            return
        now = datetime.now(timezone.utc)
        if now < _next_run() or db.uploads_today() >= config.uploads_per_day:
            return
        db.set_setting("next_run", (now + _interval()).isoformat())
        await self.run_cycle()

    @scheduler.before_loop
    async def _wait_ready(self):
        await self.wait_until_ready()

    async def run_cycle(self):
        async with self.lock:
            try:
                clip = await asyncio.to_thread(pipeline.pick_clip)
            except Exception as exc:
                log.exception("Clips zoeken mislukt")
                await self.say(f"⚠️ Clips zoeken mislukt: {_error_text(exc)}")
                return
            if clip is None:
                await self.say("🔍 Geen nieuwe clips gevonden die aan de eisen voldoen. Ik probeer het later opnieuw.")
                return

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
                return

            if config.approval_mode:
                db.mark(clip, "pending")
                await self.say(
                    f"👀 Short klaar: **{clip.title}** ({clip.broadcaster_name}). Uploaden?\n{clip.url}",
                    view=ApprovalView(self, clip, video),
                )
                return

            await self._publish(clip, video)

    async def upload(self, clip: Clip, video):
        async with self.lock:
            await self._publish(clip, video)

    async def _publish(self, clip: Clip, video):
        try:
            video_id = await asyncio.to_thread(pipeline.publish, clip, video)
        except Exception as exc:
            log.exception("Upload mislukt")
            await self.say(f"⚠️ Upload mislukt: {_error_text(exc)}")
            return
        await self.say(f"✅ Geüpload! https://youtube.com/shorts/{video_id}")


def register_commands(bot: ShortsBot):
    tree = bot.tree
    admin = app_commands.default_permissions(manage_guild=True)

    @tree.command(name="status", description="Laat zien wat de bot doet")
    @admin
    async def status(interaction: discord.Interaction):
        paused = db.get_setting("paused") == "1"
        recent = "\n".join(
            f"• {r['broadcaster']}: https://youtube.com/shorts/{r['youtube_id']}" for r in db.recent_uploads(5)
        )
        await interaction.response.send_message(
            f"**Status:** {'⏸️ gepauzeerd' if paused else '▶️ actief'}\n"
            f"**Vandaag geüpload:** {db.uploads_today()}/{config.uploads_per_day}\n"
            f"**Volgende short:** <t:{int(_next_run().timestamp())}:R>\n"
            f"**Modus:** {'goedkeuring nodig' if config.approval_mode else 'volledig automatisch'}\n"
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
        await interaction.response.send_message("▶️ Ik ga weer verder!")
