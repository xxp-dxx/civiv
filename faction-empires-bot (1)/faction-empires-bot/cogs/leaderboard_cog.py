import discord
from discord import app_commands
from discord.ext import commands

import config
import leaderboards

METRIC_CHOICES = [
    app_commands.Choice(name="Food production", value="food"),
    app_commands.Choice(name="Money production", value="money"),
    app_commands.Choice(name="Inhabitants", value="inhabitants"),
    app_commands.Choice(name="Land owned", value="land"),
]

METRIC_LABELS = {"food": "🌾 Food Production", "money": "💰 Money Production", "inhabitants": "👥 Inhabitants", "land": "🗺️ Land Owned"}
METRIC_UNITS = {"food": "/day", "money": "/day", "inhabitants": "", "land": " tiles"}


class LeaderboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    leaderboard_group = app_commands.Group(name="leaderboard", description="Server and global faction rankings.")

    @leaderboard_group.command(name="server", description="Top factions in this server by a chosen metric.")
    @app_commands.choices(metric=METRIC_CHOICES)
    async def server(self, interaction: discord.Interaction, metric: app_commands.Choice[str]):
        await interaction.response.defer()
        entries = await leaderboards.build_leaderboard(metric.value, guild_id=interaction.guild_id)
        await self._send(interaction, entries, metric.value, scope=f"in {interaction.guild.name}")

    @leaderboard_group.command(name="global", description="Top factions across every server the bot is in.")
    @app_commands.choices(metric=METRIC_CHOICES)
    async def global_(self, interaction: discord.Interaction, metric: app_commands.Choice[str]):
        await interaction.response.defer()
        entries = await leaderboards.build_leaderboard(metric.value, guild_id=None)
        await self._send(interaction, entries, metric.value, scope="globally")

    async def _send(self, interaction: discord.Interaction, entries, metric: str, scope: str):
        if not entries:
            await interaction.followup.send("No data yet - factions need to survive at least one day for stats to appear.")
            return
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, entry in enumerate(entries):
            medal = medals[i] if i < 3 else f"`{i+1}.`"
            f = entry["faction"]
            value = entry["value"]
            value_str = f"{value:.1f}" if isinstance(value, float) else str(value)
            lines.append(f"{medal} **{f['name']}** — {value_str}{METRIC_UNITS[metric]}")
        embed = discord.Embed(
            title=f"{METRIC_LABELS[metric]} Leaderboard ({scope})",
            description="\n".join(lines),
            color=config.EMBED_COLOR_DEFAULT,
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))
