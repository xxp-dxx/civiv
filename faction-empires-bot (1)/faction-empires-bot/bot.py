"""
Faction Empires - a Discord economy & faction warfare simulator.

Run with:  python bot.py
Requires:  DISCORD_BOT_TOKEN environment variable (see config.py / .env.example)
"""
import asyncio
import io
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

import config
import db
from economy import simulation
from world import map_manager

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("faction-bot")

INTENTS = discord.Intents.default()
INTENTS.members = True  # needed to resolve invited members / mentions reliably

COGS = [
    "cogs.admin_cog",
    "cogs.faction_cog",
    "cogs.map_cog",
    "cogs.military_cog",
    "cogs.diplomacy_cog",
    "cogs.leaderboard_cog",
]


class FactionBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS)

    async def setup_hook(self):
        await db.init_db()
        for cog in COGS:
            await self.load_extension(cog)
        await self.tree.sync()
        self.tick_loop.start()

    @tasks.loop(minutes=5)
    async def tick_loop(self):
        """Checks every guild's world and advances any that are due for their next day."""
        rows = await db.fetchall("SELECT * FROM guild_config")
        for cfg in rows:
            last_tick = datetime.fromisoformat(cfg["last_tick_at"]) if cfg["last_tick_at"] else None
            if last_tick is None:
                continue
            elapsed_hours = (datetime.now(timezone.utc) - last_tick).total_seconds() / 3600.0
            if elapsed_hours < cfg["tick_hours"]:
                continue
            try:
                summary = await simulation.run_tick(cfg["guild_id"])
                await self._announce_tick(cfg["guild_id"], cfg["announce_channel_id"], summary)
            except Exception:
                log.exception(f"Tick failed for guild {cfg['guild_id']}")

    @tick_loop.before_loop
    async def before_tick_loop(self):
        await self.wait_until_ready()

    async def _announce_tick(self, guild_id: int, channel_id: int | None, summary: dict):
        if not channel_id:
            return
        channel = self.get_channel(channel_id)
        if channel is None:
            return
        lines = [f"🌅 **Day {summary['day']} has begun.**"]
        if summary["disaster"]:
            d = summary["disaster"]
            lines.append(f"💥 {d['label']} struck near ({d['x']},{d['y']}) — {d['casualties']} lives lost.")
        for fid, data in summary["factions"].items():
            bits = [f"+{data['food_prod']:.0f} 🌾", f"+{data['money_prod']:.0f} 💰"]
            if data["deaths"]:
                bits.append(f"-{data['deaths']} starved")
            if data["migration"]:
                bits.append(f"{data['migration']:+d} migration")
            lines.append(f"**{data['name']}**: " + ", ".join(bits))

        try:
            png = await map_manager.render_map_png(guild_id)
            file = discord.File(fp=io.BytesIO(png), filename="map.png")
            embed = discord.Embed(description="\n".join(lines), color=config.EMBED_COLOR_DEFAULT)
            embed.set_image(url="attachment://map.png")
            await channel.send(embed=embed, file=file)
        except Exception:
            await channel.send("\n".join(lines))

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (id={self.user.id})")


async def main():
    bot = FactionBot()
    try:
        async with bot:
            await bot.start(config.BOT_TOKEN)
    finally:
        await db.close_db()


if __name__ == "__main__":
    if not config.BOT_TOKEN:
        raise SystemExit("Set the DISCORD_BOT_TOKEN environment variable before running the bot.")
    asyncio.run(main())
