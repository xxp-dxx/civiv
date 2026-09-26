import discord
from discord import app_commands
from discord.ext import commands

import config
import db
from world import map_manager
from economy import simulation


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    admin_group = app_commands.Group(name="admin", description="Server admin / setup commands.")

    @admin_group.command(name="init_world", description="Generate a brand new world map for this server. Admin only.")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(seed="Optional fixed seed for reproducible terrain", confirm_reset="Set true to confirm wiping an existing world")
    async def init_world(self, interaction: discord.Interaction, seed: int | None = None, confirm_reset: bool = False):
        existing = await map_manager.guild_has_map(interaction.guild_id)
        if existing and not confirm_reset:
            await interaction.response.send_message(
                "This server already has a world. Re-run with `confirm_reset: True` to wipe it and start over "
                "(this deletes all factions, territory and history!).",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        if existing:
            await db.execute("DELETE FROM factions WHERE guild_id=?", (interaction.guild_id,))
            await db.execute("DELETE FROM members WHERE guild_id=?", (interaction.guild_id,))
            await db.execute("DELETE FROM stats_history WHERE guild_id=?", (interaction.guild_id,))
            await db.execute("DELETE FROM map_snapshots WHERE guild_id=?", (interaction.guild_id,))
            await db.execute("DELETE FROM diplomacy WHERE guild_id=?", (interaction.guild_id,))
            await db.execute("DELETE FROM disasters_log WHERE guild_id=?", (interaction.guild_id,))
            await db.execute("DELETE FROM events_log WHERE guild_id=?", (interaction.guild_id,))
            await db.execute("DELETE FROM inhabitant_requests WHERE guild_id=?", (interaction.guild_id,))

        used_seed, w, h = await map_manager.generate_world(interaction.guild_id, seed)
        png = await map_manager.render_map_png(interaction.guild_id)
        file = discord.File(fp=__import__("io").BytesIO(png), filename="world.png")
        await interaction.followup.send(
            f"🌍 Generated a new **{w}x{h}** world (seed `{used_seed}`). "
            f"Players can now use `/faction create` to found their empires!",
            file=file,
        )

    @admin_group.command(name="set_announce_channel", description="Set the channel where daily tick summaries are posted.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_announce_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await db.execute(
            "UPDATE guild_config SET announce_channel_id=? WHERE guild_id=?", (channel.id, interaction.guild_id)
        )
        await interaction.response.send_message(f"Daily summaries will now be posted in {channel.mention}.", ephemeral=True)

    @admin_group.command(name="set_tick_hours", description="Change how many real-world hours pass between simulated days.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_tick_hours(self, interaction: discord.Interaction, hours: float):
        if hours < 0.1:
            await interaction.response.send_message("Tick interval must be at least 0.1 hours.", ephemeral=True)
            return
        await db.execute("UPDATE guild_config SET tick_hours=? WHERE guild_id=?", (hours, interaction.guild_id))
        await interaction.response.send_message(f"World will now advance one day every {hours} hour(s).", ephemeral=True)

    @admin_group.command(name="force_tick", description="Manually advance this server's world by one day (testing/dev).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def force_tick(self, interaction: discord.Interaction):
        await interaction.response.defer()
        summary = await simulation.run_tick(interaction.guild_id)
        lines = [f"**Day {summary['day']} complete.**"]
        if summary["disaster"]:
            d = summary["disaster"]
            lines.append(f"💥 {d['label']} struck near ({d['x']},{d['y']}) - {d['casualties']} casualties.")
        for fid, data in summary["factions"].items():
            lines.append(
                f"**{data['name']}**: +{data['food_prod']:.1f} food, +{data['money_prod']:.1f} money, "
                f"-{data['deaths']} deaths, {data['migration']:+d} migration"
            )
        await interaction.followup.send("\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
