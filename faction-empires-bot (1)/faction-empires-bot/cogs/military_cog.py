import discord
from discord import app_commands
from discord.ext import commands

import config
from military import manager as military_manager


class MilitaryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    military_group = app_commands.Group(name="military", description="Militia, scouting, conquest and warfare.")

    @military_group.command(name="convert", description="Convert workers into militia.")
    async def convert(self, interaction: discord.Interaction, workers: int):
        await interaction.response.defer()
        try:
            faction = await military_manager.convert_to_militia(interaction.guild_id, interaction.user.id, workers)
        except military_manager.MilitaryError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        await interaction.followup.send(
            f"⚔️ Converted {workers} workers into militia. **{faction['name']}** now has "
            f"{faction['militia_count']} militia and {faction['worker_count']} workers."
        )

    @military_group.command(name="scout", description="Scout adjacent tiles to reveal terrain and ownership.")
    async def scout(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            result = await military_manager.scout(interaction.guild_id, interaction.user.id)
        except military_manager.MilitaryError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        if not result["intel"]:
            await interaction.followup.send("Scouts found no adjacent unclaimed or enemy land - you may be surrounded, or own the whole continent!")
            return
        lines = [f"({t['x']},{t['y']}) — {t['biome']} — owner: {t['owner']}" for t in result["intel"]]
        msg = "\n".join(lines)
        if result["militia_lost"]:
            msg += f"\n\n⚠️ Lost {result['militia_lost']} militia in a scouting mishap."
        embed = discord.Embed(title="🔭 Scouting Report", description=msg, color=config.EMBED_COLOR_DEFAULT)
        await interaction.followup.send(embed=embed)

    @military_group.command(name="conquer", description="Attempt to conquer an adjacent tile (unclaimed or enemy-owned).")
    async def conquer(self, interaction: discord.Interaction, x: int, y: int):
        await interaction.response.defer()
        try:
            result = await military_manager.conquer(interaction.guild_id, interaction.user.id, x, y)
        except military_manager.MilitaryError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        if result["success"]:
            msg = f"🏴 Success! Tile ({x},{y}) [{result['tile']['biome']}] is now yours. Lost {result['militia_lost']} militia."
            if result.get("wanderers_joined"):
                msg += f"\n{len(result['wanderers_joined'])} wanderer(s) on that tile joined your faction!"
        else:
            msg = f"💀 The conquest of ({x},{y}) failed (chance was {result['chance']*100:.0f}%). Lost {result['militia_lost']} militia."
        await interaction.followup.send(msg)

    @military_group.command(name="attack", description="Attack another faction directly with committed militia.")
    async def attack(self, interaction: discord.Interaction, target_faction: str, militia: int):
        await interaction.response.defer()
        try:
            result = await military_manager.attack_faction(interaction.guild_id, interaction.user.id, target_faction, militia)
        except military_manager.MilitaryError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        if result["attacker_wins"]:
            msg = (
                f"⚔️ Victory over **{result['target']}**! Captured {result['tiles_captured']} tile(s) and "
                f"plundered {result['plunder']:.1f} money. Lost {result['attacker_losses']} militia; "
                f"enemy lost {result['defender_losses']} militia."
            )
        else:
            msg = (
                f"🩸 Defeat against **{result['target']}**. Lost {result['attacker_losses']} militia; "
                f"enemy lost {result['defender_losses']} militia."
            )
        await interaction.followup.send(msg)


async def setup(bot: commands.Bot):
    await bot.add_cog(MilitaryCog(bot))
