import discord
from discord import app_commands
from discord.ext import commands

import config
import db
from factions import manager as faction_manager
from economy import simulation


class ConfirmView(discord.ui.View):
    """Generic yes/no confirmation restricted to a specific user, used for invites & join requests."""

    def __init__(self, target_user_id: int, on_confirm, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.target_user_id = target_user_id
        self.on_confirm = on_confirm
        self.result: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target_user_id:
            await interaction.response.send_message("This isn't your confirmation to answer.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = True
        self.stop()
        try:
            await self.on_confirm(interaction)
        except Exception as e:  # noqa
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = False
        self.stop()
        await interaction.response.edit_message(content="Declined.", view=None, embed=None)


def faction_embed(faction, inhabitants: int, land: int) -> discord.Embed:
    color = int(faction["color_hex"].lstrip("#"), 16)
    e = discord.Embed(title=f"🏰 {faction['name']}", color=color)
    e.add_field(name="Leader", value=f"<@{faction['leader_id']}>", inline=True)
    e.add_field(name="Color", value=faction["color_hex"], inline=True)
    e.add_field(name="Land", value=str(land), inline=True)
    e.add_field(name="Workers", value=str(faction["worker_count"]), inline=True)
    e.add_field(name="Militia", value=str(faction["militia_count"]), inline=True)
    e.add_field(name="Inhabitants", value=str(inhabitants), inline=True)
    e.add_field(name="Treasury", value=f"{faction['treasury']:.1f} 💰", inline=True)
    e.add_field(name="Food stock", value=f"{faction['food_stock']:.1f} 🌾", inline=True)
    return e


class FactionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    faction_group = app_commands.Group(name="faction", description="Create and manage your faction.")

    @faction_group.command(name="create", description="Found a new faction with a starter plot of land.")
    @app_commands.describe(name="Your faction's name", color="Hex color for your territory, e.g. #3498DB")
    async def create(self, interaction: discord.Interaction, name: str, color: str):
        await interaction.response.defer()
        try:
            faction = await faction_manager.create_faction(interaction.guild_id, interaction.user.id, name, color)
        except faction_manager.FactionError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        inhabitants = await faction_manager.total_inhabitants(faction)
        from world import map_manager
        land = await map_manager.count_faction_land(interaction.guild_id, faction["faction_id"])
        await interaction.followup.send(
            f"🎉 <@{interaction.user.id}> founded **{faction['name']}**!", embed=faction_embed(faction, inhabitants, land)
        )

    @faction_group.command(name="info", description="View info about a faction (yours by default).")
    async def info(self, interaction: discord.Interaction, name: str | None = None):
        await interaction.response.defer()
        if name:
            faction = await faction_manager.get_faction_by_name(interaction.guild_id, name)
        else:
            faction = await faction_manager.get_member_faction(interaction.guild_id, interaction.user.id)
        if not faction:
            await interaction.followup.send("No such faction found (or you're not in one - try `/faction info <name>`).", ephemeral=True)
            return
        inhabitants = await faction_manager.total_inhabitants(faction)
        from world import map_manager
        land = await map_manager.count_faction_land(faction["guild_id"], faction["faction_id"])
        await interaction.followup.send(embed=faction_embed(faction, inhabitants, land))

    @faction_group.command(name="list", description="List all active factions in this server.")
    async def list_factions(self, interaction: discord.Interaction):
        await interaction.response.defer()
        factions = await faction_manager.list_active_factions(interaction.guild_id)
        if not factions:
            await interaction.followup.send("No factions exist yet - be the first with `/faction create`!")
            return
        from world import map_manager
        lines = []
        for f in factions:
            inhabitants = await faction_manager.total_inhabitants(f)
            land = await map_manager.count_faction_land(interaction.guild_id, f["faction_id"])
            lines.append(f"**{f['name']}** ({f['color_hex']}) - Leader: <@{f['leader_id']}> - {inhabitants} inhabitants, {land} tiles")
        embed = discord.Embed(title="Factions of this server", description="\n".join(lines), color=config.EMBED_COLOR_DEFAULT)
        await interaction.followup.send(embed=embed)

    @faction_group.command(name="invite", description="Invite a member (decisioner) into your faction. They must accept.")
    async def invite(self, interaction: discord.Interaction, user: discord.Member):
        faction = await faction_manager.get_member_faction(interaction.guild_id, interaction.user.id)
        if not faction:
            await interaction.response.send_message("You must be in a faction to invite people.", ephemeral=True)
            return

        async def on_confirm(confirm_interaction: discord.Interaction):
            try:
                await faction_manager.invite_member(interaction.guild_id, interaction.user.id, user.id)
            except faction_manager.FactionError as e:
                await confirm_interaction.response.edit_message(content=f"❌ {e}", view=None)
                return
            await confirm_interaction.response.edit_message(
                content=f"✅ {user.mention} has joined **{faction['name']}**!", view=None
            )

        view = ConfirmView(target_user_id=user.id, on_confirm=on_confirm)
        await interaction.response.send_message(
            f"{user.mention}, **{faction['name']}** (led by <@{faction['leader_id']}>) has invited you to join as a decisioner. Accept?",
            view=view,
        )

    @faction_group.command(name="join", description="Request to join an existing faction (leader must approve).")
    async def join(self, interaction: discord.Interaction, name: str):
        faction = await faction_manager.get_faction_by_name(interaction.guild_id, name)
        if not faction:
            await interaction.response.send_message("No such faction.", ephemeral=True)
            return
        member = await faction_manager.get_member(interaction.guild_id, interaction.user.id)
        if member["faction_id"]:
            await interaction.response.send_message("Leave your current faction first with `/faction leave`.", ephemeral=True)
            return

        async def on_confirm(confirm_interaction: discord.Interaction):
            try:
                await faction_manager.invite_member(interaction.guild_id, faction["leader_id"], interaction.user.id)
            except faction_manager.FactionError as e:
                await confirm_interaction.response.edit_message(content=f"❌ {e}", view=None)
                return
            await confirm_interaction.response.edit_message(
                content=f"✅ <@{interaction.user.id}> has joined **{faction['name']}**!", view=None
            )

        view = ConfirmView(target_user_id=faction["leader_id"], on_confirm=on_confirm)
        await interaction.response.send_message(
            f"<@{faction['leader_id']}>, {interaction.user.mention} wants to join **{faction['name']}**. Approve?",
            view=view,
        )

    @faction_group.command(name="leave", description="Leave your current faction (becoming a wanderer).")
    async def leave(self, interaction: discord.Interaction):
        try:
            await faction_manager.leave_faction(interaction.guild_id, interaction.user.id)
        except faction_manager.FactionError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message("You've left your faction and are now wandering the map.")

    @faction_group.command(name="disband", description="[Leader] Permanently disband your faction.")
    async def disband(self, interaction: discord.Interaction):
        try:
            await faction_manager.disband_faction(interaction.guild_id, interaction.user.id)
        except faction_manager.FactionError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message("Your faction has been disbanded. Its land is now unclaimed.")

    @faction_group.command(
        name="transfer",
        description="[Leader] Give control to a member of your faction, or merge into another faction's leader.",
    )
    async def transfer(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer()
        try:
            result = await faction_manager.transfer_leadership(interaction.guild_id, interaction.user.id, user.id)
        except faction_manager.FactionError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        if result["type"] == "handoff":
            await interaction.followup.send(f"👑 Leadership of **{result['faction']['name']}** was passed to {user.mention}.")
        else:
            await interaction.followup.send(
                f"🤝 **{result['old_faction']['name']}** has been incorporated into "
                f"**{result['into_faction']['name']}**! All territory, workers and treasury merged."
            )

    @faction_group.command(name="stats", description="[Leader] Detailed production & population analytics.")
    async def stats(self, interaction: discord.Interaction):
        await interaction.response.defer()
        faction = await faction_manager.get_member_faction(interaction.guild_id, interaction.user.id)
        if not faction:
            await interaction.followup.send("You're not in a faction.", ephemeral=True)
            return
        member = await faction_manager.get_member(interaction.guild_id, interaction.user.id)
        if member["role"] not in ("leader", "officer"):
            await interaction.followup.send("Only the leader or an officer can view detailed stats.", ephemeral=True)
            return

        dash = await simulation.get_faction_dashboard(faction["faction_id"], days=7)
        embed = discord.Embed(title=f"📊 {faction['name']} — Leadership Dashboard", color=config.EMBED_COLOR_DEFAULT)
        embed.add_field(name="Inhabitants", value=str(dash["inhabitants"]), inline=True)
        embed.add_field(name="Land", value=str(dash["land"]), inline=True)
        embed.add_field(name="Food / capita (last day)", value=f"{dash['food_per_capita']:.3f}", inline=True)
        embed.add_field(name="Money / capita (last day)", value=f"{dash['money_per_capita']:.3f}", inline=True)

        if dash["history"]:
            lines = []
            for row in dash["history"]:
                sign = "+" if row["inhabitants_delta"] >= 0 else ""
                lines.append(
                    f"Day {row['day']}: {sign}{row['inhabitants_delta']} inhabitants, "
                    f"{row['food_prod']:.1f} food, {row['money_prod']:.1f} money, {row['land_count']} tiles"
                )
            embed.add_field(name="Last 7 days", value="\n".join(lines), inline=False)
        await interaction.followup.send(embed=embed)

    @faction_group.command(name="members", description="List the decisioners (real players) in a faction.")
    async def members(self, interaction: discord.Interaction, name: str | None = None):
        await interaction.response.defer()
        if name:
            faction = await faction_manager.get_faction_by_name(interaction.guild_id, name)
        else:
            faction = await faction_manager.get_member_faction(interaction.guild_id, interaction.user.id)
        if not faction:
            await interaction.followup.send("No such faction.", ephemeral=True)
            return
        members = await faction_manager.list_faction_members(faction["faction_id"])
        lines = [f"<@{m['user_id']}> - {m['role']}" for m in members]
        embed = discord.Embed(
            title=f"Decisioners of {faction['name']}",
            description="\n".join(lines) if lines else "No decisioners yet - only simulated workers.",
            color=config.EMBED_COLOR_DEFAULT,
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(FactionCog(bot))
