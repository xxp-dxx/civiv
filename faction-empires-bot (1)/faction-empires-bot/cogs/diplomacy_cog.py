import discord
from discord import app_commands
from discord.ext import commands

import config
from diplomacy import manager as diplomacy_manager
from factions import manager as faction_manager


class DiplomacyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    diplomacy_group = app_commands.Group(name="diplomacy", description="Alliances and trade pacts between factions.")

    @diplomacy_group.command(name="propose_alliance", description="[Leader] Propose a mutual-defense alliance.")
    async def propose_alliance(self, interaction: discord.Interaction, target_faction: str):
        await self._propose(interaction, target_faction, "alliance")

    @diplomacy_group.command(name="propose_trade", description="[Leader] Propose a trade pact (shares surplus food/money each day).")
    async def propose_trade(self, interaction: discord.Interaction, target_faction: str):
        await self._propose(interaction, target_faction, "trade")

    async def _propose(self, interaction: discord.Interaction, target_faction: str, kind: str):
        await interaction.response.defer()
        try:
            await diplomacy_manager.propose(interaction.guild_id, interaction.user.id, target_faction, kind)
        except diplomacy_manager.DiplomacyError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        await interaction.followup.send(f"📜 Proposed a {kind} with **{target_faction}**. They must `/diplomacy accept`.")

    @diplomacy_group.command(name="accept", description="[Leader] Accept a pending alliance or trade proposal.")
    async def accept(self, interaction: discord.Interaction, from_faction: str, kind: str):
        await interaction.response.defer()
        if kind not in ("alliance", "trade"):
            await interaction.followup.send("kind must be 'alliance' or 'trade'.", ephemeral=True)
            return
        try:
            await diplomacy_manager.accept(interaction.guild_id, interaction.user.id, from_faction, kind)
        except diplomacy_manager.DiplomacyError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        await interaction.followup.send(f"🤝 {kind.title()} with **{from_faction}** is now active!")

    @diplomacy_group.command(name="break", description="[Leader] Break diplomatic ties with another faction.")
    async def break_pact(self, interaction: discord.Interaction, target_faction: str, kind: str | None = None):
        await interaction.response.defer()
        try:
            n = await diplomacy_manager.break_pact(interaction.guild_id, interaction.user.id, target_faction, kind)
        except diplomacy_manager.DiplomacyError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        await interaction.followup.send(f"💔 Broke {n} diplomatic relationship(s) with **{target_faction}**.")

    @diplomacy_group.command(name="list", description="List your faction's current diplomatic relationships.")
    async def list_relations(self, interaction: discord.Interaction):
        await interaction.response.defer()
        faction = await faction_manager.get_member_faction(interaction.guild_id, interaction.user.id)
        if not faction:
            await interaction.followup.send("You're not in a faction.", ephemeral=True)
            return
        relations = await diplomacy_manager.list_relations(interaction.guild_id, faction["faction_id"])
        if not relations:
            await interaction.followup.send("No diplomatic relationships yet.")
            return
        lines = []
        for r in relations:
            other_id = r["faction_b"] if r["faction_a"] == faction["faction_id"] else r["faction_a"]
            other = await faction_manager.get_faction_by_id(other_id)
            oname = other["name"] if other else "(dissolved)"
            lines.append(f"{r['kind'].title()} with **{oname}** - {r['status']}")
        embed = discord.Embed(title=f"Diplomacy — {faction['name']}", description="\n".join(lines), color=config.EMBED_COLOR_DEFAULT)
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(DiplomacyCog(bot))
