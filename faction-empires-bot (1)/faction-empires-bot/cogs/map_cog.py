import io
import discord
from discord import app_commands
from discord.ext import commands

import config
import db
from world import map_manager
from factions import manager as faction_manager


class MapCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    map_group = app_commands.Group(
        name="map",
        description="View and navigate the world map.",
    )

    @map_group.command(
        name="view",
        description="Render today's world map with faction territories.",
    )
    async def view(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await map_manager.guild_has_map(interaction.guild_id):
            await interaction.followup.send(
                "This server has no world yet - ask an admin to initialize one."
            )
            return

        png = await map_manager.render_map_png(interaction.guild_id)
        cfg = await db.fetchone(
            "SELECT day_count FROM guild_config WHERE guild_id=?",
            (interaction.guild_id,),
        )
        file = discord.File(fp=io.BytesIO(png), filename="map.png")
        embed = discord.Embed(
            title=f"🗺️ World Map — Day {cfg['day_count']}",
            color=config.EMBED_COLOR_DEFAULT,
        )
        embed.set_image(url="attachment://map.png")
        await interaction.followup.send(embed=embed, file=file)

    @map_group.command(
        name="history",
        description="List the days for which a historical map snapshot exists.",
    )
    async def history(self, interaction: discord.Interaction):
        await interaction.response.defer()
        days = await map_manager.list_snapshot_days(interaction.guild_id)
        if not days:
            await interaction.followup.send("No snapshots recorded yet.")
            return

        await interaction.followup.send(
            f"Snapshots available for days: {', '.join(map(str, days[:30]))}"
        )

    @map_group.command(
        name="snapshot",
        description="Show faction ownership stats from a specific past day.",
    )
    async def snapshot(self, interaction: discord.Interaction, day: int):
        await interaction.response.defer()
        data = await map_manager.load_snapshot(interaction.guild_id, day)
        if not data:
            await interaction.followup.send(f"No snapshot found for day {day}.")
            return

        owner_counts: dict[int, int] = {}
        for tile in data:
            if tile["owner"]:
                owner_counts[tile["owner"]] = owner_counts.get(tile["owner"], 0) + 1

        lines = []
        for faction_id, count in sorted(
            owner_counts.items(),
            key=lambda item: -item[1],
        ):
            faction = await faction_manager.get_faction_by_id(faction_id)
            name = faction["name"] if faction else f"(dissolved #{faction_id})"
            lines.append(f"**{name}**: {count} tiles")

        embed = discord.Embed(
            title=f"Territory on Day {day}",
            description="\n".join(lines) or "No claimed territory.",
            color=config.EMBED_COLOR_DEFAULT,
        )
        await interaction.followup.send(embed=embed)

    @map_group.command(
        name="whereami",
        description="Show your wanderer location, if you have no faction.",
    )
    async def whereami(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        member = await faction_manager.get_member(
            interaction.guild_id,
            interaction.user.id,
        )
        if member["faction_id"]:
            await interaction.followup.send(
                "You belong to a faction, so you're not a wanderer.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"You're wandering at tile ({member['wander_x']}, {member['wander_y']}). "
            "If a faction conquers that tile, you'll automatically join them.",
            ephemeral=True,
        )

    @map_group.command(
        name="move",
        description="Move your wanderer one tile north, south, east, or west.",
    )
    @app_commands.choices(
        direction=[
            app_commands.Choice(name="North", value="north"),
            app_commands.Choice(name="South", value="south"),
            app_commands.Choice(name="West", value="west"),
            app_commands.Choice(name="East", value="east"),
        ]
    )
    async def move(
        self,
        interaction: discord.Interaction,
        direction: app_commands.Choice[str],
    ):
        try:
            tile = await faction_manager.move_wanderer(
                interaction.guild_id,
                interaction.user.id,
                direction.value,
            )
        except faction_manager.FactionError as e:
            await interaction.response.send_message(
                f"❌ {e}",
                ephemeral=True,
            )
            return

        owner = "Unclaimed"
        if tile["owner_faction_id"]:
            faction = await faction_manager.get_faction_by_id(
                tile["owner_faction_id"]
            )
            owner = faction["name"] if faction else "Unknown"

        await interaction.response.send_message(
            f"Moved {direction.name.lower()} to ({tile['x']},{tile['y']}) — "
            f"{tile['biome']}, owner: **{owner}**."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MapCog(bot))
