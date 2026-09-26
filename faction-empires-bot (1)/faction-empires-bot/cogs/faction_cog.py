import discord
from discord import app_commands
from discord.ext import commands

import config
from factions import manager as faction_manager
from economy import simulation
from world import map_manager


class ConfirmView(discord.ui.View):
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
        except Exception as e:
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = False
        self.stop()
        await interaction.response.edit_message(content="Declined.", view=None, embed=None)


class InhabitantRequestModal(discord.ui.Modal, title="Request Inhabitants"):
    inhabitants = discord.ui.TextInput(
        label="Workers requested",
        placeholder="How many simulated inhabitants should move to your faction?",
        min_length=1,
        max_length=8,
        required=True,
    )
    food_offer = discord.ui.TextInput(
        label="Food offered",
        placeholder="Food sent in exchange (0 allowed)",
        default="0",
        min_length=1,
        max_length=20,
        required=True,
    )
    treasury_offer = discord.ui.TextInput(
        label="Money offered",
        placeholder="Money sent in exchange (0 allowed)",
        default="0",
        min_length=1,
        max_length=20,
        required=True,
    )

    def __init__(self, guild_id: int, requester_user_id: int, donor_faction_name: str):
        super().__init__()
        self.guild_id = guild_id
        self.requester_user_id = requester_user_id
        self.donor_faction_name = donor_faction_name

    async def on_submit(self, interaction: discord.Interaction):
        try:
            inhabitants = int(str(self.inhabitants.value).strip())
            food = float(str(self.food_offer.value).strip())
            money = float(str(self.treasury_offer.value).strip())
            request = await faction_manager.create_inhabitant_request(
                self.guild_id,
                self.requester_user_id,
                self.donor_faction_name,
                inhabitants,
                food,
                money,
            )
        except (ValueError, faction_manager.FactionError) as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        requester = await faction_manager.get_member_faction(self.guild_id, self.requester_user_id)
        donor = await faction_manager.get_faction_by_id(request["donor_faction_id"])
        if not requester or not donor:
            await interaction.response.send_message(
                "The request was created, but its factions could not be loaded.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Population Transfer Request",
            description=(
                f"**{requester['name']}** wants **{request['inhabitants']} workers** from "
                f"**{donor['name']}**.\n"
                f"Offer: **{request['food_offer']:.1f} food** and "
                f"**{request['treasury_offer']:.1f} money**."
            ),
            color=int(requester["color_hex"].lstrip("#"), 16),
        )
        embed.add_field(name="Requested from", value=f"<@{donor['leader_id']}>", inline=False)
        embed.set_footer(text=f"Request #{request['request_id']}")

        await interaction.response.send_message(
            f"<@{donor['leader_id']}> — population transfer request incoming.",
            embed=embed,
            view=InhabitantRequestView(self.guild_id, request["request_id"]),
        )


class InhabitantRequestView(discord.ui.View):
    def __init__(self, guild_id: int, request_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.request_id = request_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            result = await faction_manager.resolve_inhabitant_request(
                self.guild_id, interaction.user.id, self.request_id, True
            )
        except faction_manager.FactionError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        self.stop()
        request = result["request"]
        await interaction.response.edit_message(
            content=f"✅ {request['inhabitants']} workers transferred successfully.",
            embed=None,
            view=None,
        )

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await faction_manager.resolve_inhabitant_request(
                self.guild_id, interaction.user.id, self.request_id, False
            )
        except faction_manager.FactionError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        self.stop()
        await interaction.response.edit_message(
            content="❌ Population transfer request declined.",
            embed=None,
            view=None,
        )


async def faction_embed(faction, land: int) -> discord.Embed:
    economy = await simulation.get_faction_economy(faction)
    color = int(faction["color_hex"].lstrip("#"), 16)
    embed = discord.Embed(title=f"🏰 {faction['name']}", color=color)
    embed.add_field(name="Leader", value=f"<@{faction['leader_id']}>", inline=True)
    embed.add_field(name="Land", value=str(land), inline=True)
    embed.add_field(name="Workers", value=str(faction["worker_count"]), inline=True)
    embed.add_field(name="Decisioners", value=str(economy["decisioners"]), inline=True)
    embed.add_field(name="Militia", value=str(faction["militia_count"]), inline=True)
    embed.add_field(name="Inhabitants", value=str(economy["inhabitants"]), inline=True)
    embed.add_field(name="Treasury", value=f"{faction['treasury']:.1f} 💰", inline=True)
    embed.add_field(name="Food stock", value=f"{faction['food_stock']:.1f} 🌾", inline=True)
    embed.add_field(name="Food production / day", value=f"{economy['food_prod']:.1f} 🌾", inline=True)
    embed.add_field(name="Food consumption / day", value=f"{economy['food_consumption']:.1f} 🌾", inline=True)
    embed.add_field(name="Militia food upkeep / day", value=f"{economy['militia_food_upkeep']:.1f} 🌾", inline=True)
    embed.add_field(name="Net food / day", value=f"{economy['net_food']:+.1f} 🌾", inline=True)
    reserves = "∞" if economy["food_days"] == float("inf") else f"{economy['food_days']:.1f} days"
    embed.add_field(name="Food reserves", value=reserves, inline=True)
    embed.add_field(name="Natural growth", value=f"{economy['growth_percent']:.3f}% / day", inline=True)
    status = "⚠️ CRISIS — no workers producing resources" if economy["crisis"] else "Stable"
    embed.add_field(name="Status", value=status, inline=False)
    return embed


class FactionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    faction_group = app_commands.Group(
        name="faction",
        description="Create and manage your faction.",
    )

    @faction_group.command(name="create", description="Found a new faction with a starter plot of land.")
    @app_commands.describe(
        name="Your faction's name",
        color="Hex color for your territory, e.g. #3498DB",
    )
    async def create(self, interaction: discord.Interaction, name: str, color: str):
        await interaction.response.defer()
        try:
            faction = await faction_manager.create_faction(
                interaction.guild_id, interaction.user.id, name, color
            )
        except faction_manager.FactionError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        land = await map_manager.count_faction_land(
            interaction.guild_id, faction["faction_id"]
        )
        await interaction.followup.send(
            f"🎉 <@{interaction.user.id}> founded **{faction['name']}**!",
            embed=await faction_embed(faction, land),
        )

    @faction_group.command(
        name="info",
        description="View detailed information about a faction.",
    )
    async def info(self, interaction: discord.Interaction, name: str | None = None):
        await interaction.response.defer()
        faction = (
            await faction_manager.get_faction_by_name(interaction.guild_id, name)
            if name
            else await faction_manager.get_member_faction(
                interaction.guild_id, interaction.user.id
            )
        )
        if not faction:
            await interaction.followup.send(
                "No such faction found, or you are not in one.",
                ephemeral=True,
            )
            return

        land = await map_manager.count_faction_land(
            faction["guild_id"], faction["faction_id"]
        )
        await interaction.followup.send(embed=await faction_embed(faction, land))

    @faction_group.command(name="list", description="List all active factions in this server.")
    async def list_factions(self, interaction: discord.Interaction):
        await interaction.response.defer()
        factions = await faction_manager.list_active_factions(interaction.guild_id)
        if not factions:
            await interaction.followup.send("No factions exist yet - be the first to found one.")
            return

        lines = []
        for faction in factions:
            economy = await simulation.get_faction_economy(faction)
            land = await map_manager.count_faction_land(
                interaction.guild_id, faction["faction_id"]
            )
            lines.append(
                f"**{faction['name']}** ({faction['color_hex']}) - "
                f"Leader: <@{faction['leader_id']}> - "
                f"{economy['inhabitants']} inhabitants, {land} tiles, "
                f"{faction['food_stock']:.1f} food"
            )

        embed = discord.Embed(
            title="Factions of this server",
            description="\n".join(lines),
            color=config.EMBED_COLOR_DEFAULT,
        )
        await interaction.followup.send(embed=embed)

    @faction_group.command(
        name="invite",
        description="Invite a member as a decisioner. They must accept.",
    )
    async def invite(self, interaction: discord.Interaction, user: discord.Member):
        faction = await faction_manager.get_member_faction(
            interaction.guild_id, interaction.user.id
        )
        if not faction:
            await interaction.response.send_message(
                "You must be in a faction to invite people.",
                ephemeral=True,
            )
            return

        async def on_confirm(confirm_interaction: discord.Interaction):
            try:
                await faction_manager.invite_member(
                    interaction.guild_id,
                    interaction.user.id,
                    user.id,
                )
            except faction_manager.FactionError as e:
                await confirm_interaction.response.edit_message(
                    content=f"❌ {e}",
                    view=None,
                )
                return

            await confirm_interaction.response.edit_message(
                content=f"✅ {user.mention} has joined **{faction['name']}**!",
                view=None,
            )

        await interaction.response.send_message(
            f"{user.mention}, **{faction['name']}** has invited you to join as a decisioner. Accept?",
            view=ConfirmView(target_user_id=user.id, on_confirm=on_confirm),
        )

    @faction_group.command(
        name="join",
        description="Request to join an existing faction.",
    )
    async def join(self, interaction: discord.Interaction, name: str):
        faction = await faction_manager.get_faction_by_name(
            interaction.guild_id, name
        )
        if not faction:
            await interaction.response.send_message(
                "No such faction.",
                ephemeral=True,
            )
            return

        member = await faction_manager.get_member(
            interaction.guild_id, interaction.user.id
        )
        if member["faction_id"]:
            await interaction.response.send_message(
                "Leave your current faction first.",
                ephemeral=True,
            )
            return

        async def on_confirm(confirm_interaction: discord.Interaction):
            try:
                await faction_manager.invite_member(
                    interaction.guild_id,
                    faction["leader_id"],
                    interaction.user.id,
                )
            except faction_manager.FactionError as e:
                await confirm_interaction.response.edit_message(
                    content=f"❌ {e}",
                    view=None,
                )
                return

            await confirm_interaction.response.edit_message(
                content=f"✅ <@{interaction.user.id}> has joined **{faction['name']}**!",
                view=None,
            )

        await interaction.response.send_message(
            f"<@{faction['leader_id']}>, {interaction.user.mention} wants to join **{faction['name']}**. Approve?",
            view=ConfirmView(
                target_user_id=faction["leader_id"],
                on_confirm=on_confirm,
            ),
        )

    @faction_group.command(
        name="request_inhabitants",
        description="Ask another faction for workers in exchange for resources.",
    )
    async def request_inhabitants(
        self,
        interaction: discord.Interaction,
        target_faction: str,
    ):
        if not await faction_manager.get_member_faction(
            interaction.guild_id, interaction.user.id
        ):
            await interaction.response.send_message(
                "You must be in a faction to make a population request.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            InhabitantRequestModal(
                interaction.guild_id,
                interaction.user.id,
                target_faction,
            )
        )

    @faction_group.command(
        name="leave",
        description="Leave your current faction and become a wanderer.",
    )
    async def leave(self, interaction: discord.Interaction):
        try:
            await faction_manager.leave_faction(
                interaction.guild_id,
                interaction.user.id,
            )
        except faction_manager.FactionError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(
            "You've left your faction and are now wandering the map."
        )

    @faction_group.command(
        name="disband",
        description="[Leader] Permanently disband your faction.",
    )
    async def disband(self, interaction: discord.Interaction):
        try:
            await faction_manager.disband_faction(
                interaction.guild_id,
                interaction.user.id,
            )
        except faction_manager.FactionError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(
            "Your faction has been disbanded. Its land is now unclaimed."
        )

    @faction_group.command(
        name="transfer",
        description="[Leader] Give control to a member, or merge into another faction's leader.",
    )
    async def transfer(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        await interaction.response.defer()
        try:
            result = await faction_manager.transfer_leadership(
                interaction.guild_id,
                interaction.user.id,
                user.id,
            )
        except faction_manager.FactionError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        if result["type"] == "handoff":
            await interaction.followup.send(
                f"👑 Leadership of **{result['faction']['name']}** was passed to {user.mention}."
            )
        else:
            await interaction.followup.send(
                f"🤝 **{result['old_faction']['name']}** has been incorporated into "
                f"**{result['into_faction']['name']}**! All territory, workers and treasury merged."
            )

    @faction_group.command(
        name="stats",
        description="[Leader] Detailed production and population analytics.",
    )
    async def stats(self, interaction: discord.Interaction):
        await interaction.response.defer()
        faction = await faction_manager.get_member_faction(
            interaction.guild_id, interaction.user.id
        )
        if not faction:
            await interaction.followup.send("You're not in a faction.", ephemeral=True)
            return

        member = await faction_manager.get_member(
            interaction.guild_id, interaction.user.id
        )
        if member["role"] not in ("leader", "officer"):
            await interaction.followup.send(
                "Only the leader or an officer can view detailed stats.",
                ephemeral=True,
            )
            return

        dash = await simulation.get_faction_dashboard(
            faction["faction_id"],
            days=7,
        )
        embed = discord.Embed(
            title=f"📊 {faction['name']} — Leadership Dashboard",
            color=config.EMBED_COLOR_DEFAULT,
        )
        embed.add_field(name="Inhabitants", value=str(dash["inhabitants"]), inline=True)
        embed.add_field(name="Land", value=str(dash["land"]), inline=True)
        embed.add_field(
            name="Food / capita (last day)",
            value=f"{dash['food_per_capita']:.3f}",
            inline=True,
        )
        embed.add_field(
            name="Money / capita (last day)",
            value=f"{dash['money_per_capita']:.3f}",
            inline=True,
        )

        if dash["history"]:
            lines = []
            for row in dash["history"]:
                sign = "+" if row["inhabitants_delta"] >= 0 else ""
                lines.append(
                    f"Day {row['day']}: {sign}{row['inhabitants_delta']} inhabitants, "
                    f"{row['food_prod']:.1f} food, {row['money_prod']:.1f} money, "
                    f"{row['land_count']} tiles"
                )
            embed.add_field(
                name="Last 7 days",
                value="\n".join(lines),
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    @faction_group.command(
        name="members",
        description="List the decisioners in a faction.",
    )
    async def members(
        self,
        interaction: discord.Interaction,
        name: str | None = None,
    ):
        await interaction.response.defer()
        faction = (
            await faction_manager.get_faction_by_name(interaction.guild_id, name)
            if name
            else await faction_manager.get_member_faction(
                interaction.guild_id,
                interaction.user.id,
            )
        )
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
