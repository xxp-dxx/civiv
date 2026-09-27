import discord
from discord import app_commands
from discord.ext import commands

from factions import manager as faction_manager
from military import manager as military_manager
from world import map_manager


class ConquestMilitiaModal(discord.ui.Modal, title="Conquest Force"):
    militia = discord.ui.TextInput(
        label="Militia per tile",
        placeholder="Enter the exact militia force for each selected tile",
        min_length=1,
        max_length=8,
        required=True,
    )

    def __init__(
        self,
        guild_id: int,
        user_id: int,
        targets: list[tuple[int, int]],
    ):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.targets = targets

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(str(self.militia.value).strip())
            results = await military_manager.conquer_multiple(
                self.guild_id,
                self.user_id,
                self.targets,
                amount,
            )
        except (ValueError, military_manager.MilitaryError) as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        successes = [r for r in results if r["success"]]
        total_lost = sum(r["militia_lost"] for r in results)
        lines = [
            f"🏴 **Conquest resolved:** {len(successes)}/{len(results)} tile(s) conquered.",
            f"Committed {amount} militia per tile; lost {total_lost} total.",
        ]

        for result in results:
            x, y = result["tile"]["x"], result["tile"]["y"]
            if result["success"]:
                lines.append(
                    f"• ({x},{y}) [{result['tile']['biome']}] **success** — "
                    f"{result['militia_lost']} militia lost."
                )
                if result.get("wanderers_joined"):
                    lines.append(
                        f"  {len(result['wanderers_joined'])} wanderer(s) joined your faction."
                    )
            else:
                lines.append(
                    f"• ({x},{y}) [{result['tile']['biome']}] **failed** — "
                    f"{result['militia_lost']} militia lost, "
                    f"{result['chance'] * 100:.0f}% estimated chance."
                )

        await interaction.response.send_message("\n".join(lines))


class ConquestView(discord.ui.View):
    PAGE_SIZE = 25

    def __init__(
        self,
        owner_id: int,
        candidates: list[dict],
        max_tiles: int,
    ):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.candidates = candidates
        self.max_tiles = min(max_tiles, self.PAGE_SIZE)
        self.page = 0

        self.target_select = discord.ui.Select(
            placeholder=f"Choose up to {self.max_tiles} frontier tile(s)",
            min_values=1,
            max_values=self.max_tiles,
        )
        self.add_item(self.target_select)
        self.target_select.callback = self._select_target
        self._refresh()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This conquest menu belongs to someone else.",
                ephemeral=True,
            )
            return False
        return True

    def _page_items(self):
        start = self.page * self.PAGE_SIZE
        return self.candidates[start:start + self.PAGE_SIZE]

    def _refresh(self):
        options = []
        for item in self._page_items():
            owner = item["owner_name"]
            label = f"({item['x']},{item['y']}) · {item['biome']}"
            description = (
                f"Owned by {owner}"
                if owner != "Unclaimed"
                else "Unclaimed land"
            )
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    description=description[:100],
                    value=f"{item['x']}:{item['y']}",
                )
            )

        self.target_select.options = options
        if hasattr(self, "previous"):
            self.previous.disabled = self.page <= 0
        if hasattr(self, "next"):
            self.next.disabled = (
                (self.page + 1) * self.PAGE_SIZE >= len(self.candidates)
            )

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._refresh()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._refresh()
        await interaction.response.edit_message(view=self)

    async def _select_target(self, interaction: discord.Interaction):
        targets = [
            tuple(int(value) for value in raw.split(":", 1))
            for raw in self.target_select.values
        ]
        self.stop()
        await interaction.response.send_modal(
            ConquestMilitiaModal(
                interaction.guild_id,
                interaction.user.id,
                targets,
            )
        )

class AttackMilitiaModal(discord.ui.Modal, title="Commit Attack Force"):
    militia = discord.ui.TextInput(
        label="Militia to commit",
        placeholder="Enter the exact number of militia to commit",
        min_length=1,
        max_length=8,
        required=True,
    )

    def __init__(self, guild_id: int, user_id: int, target_faction: str):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.target_faction = target_faction

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(str(self.militia.value).strip())
            result = await military_manager.attack_faction(
                self.guild_id,
                self.user_id,
                self.target_faction,
                amount,
            )
        except (ValueError, military_manager.MilitaryError) as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        if result["attacker_wins"]:
            message = (
                f"⚔️ Victory over **{result['target']}**. "
                f"Captured {result['tiles_captured']} tile(s) and "
                f"plundered {result['plunder']:.1f} money. "
                f"Lost {result['attacker_losses']} militia; "
                f"enemy lost {result['defender_losses']}."
            )
        else:
            message = (
                f"🩸 Defeat against **{result['target']}**. "
                f"Lost {result['attacker_losses']} militia; "
                f"enemy lost {result['defender_losses']}."
            )

        await interaction.response.send_message(message)


class MilitaryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    military_group = app_commands.Group(
        name="military",
        description="Militia, conquest and warfare.",
    )

    @military_group.command(
        name="convert",
        description="Convert workers into militia.",
    )
    async def convert(self, interaction: discord.Interaction, workers: int):
        await interaction.response.defer()
        try:
            faction = await military_manager.convert_to_militia(
                interaction.guild_id,
                interaction.user.id,
                workers,
            )
        except military_manager.MilitaryError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        await interaction.followup.send(
            f"⚔️ Converted {workers} workers into militia. "
            f"**{faction['name']}** now has {faction['militia_count']} militia "
            f"and {faction['worker_count']} workers."
        )

    @military_group.command(
        name="conquer",
        description="Choose one or more adjacent tiles, then choose the militia force per tile.",
    )
    async def conquer(self, interaction: discord.Interaction):
        faction = await faction_manager.get_member_faction(
            interaction.guild_id,
            interaction.user.id,
        )
        if not faction:
            await interaction.response.send_message(
                "You must be in a faction to conquer land.",
                ephemeral=True,
            )
            return

        candidates = await map_manager.get_adjacent_unclaimed_or_enemy(
            interaction.guild_id,
            faction["faction_id"],
        )
        max_tiles = military_manager.get_conquest_tile_limit(faction["militia_count"])
        if max_tiles <= 0:
            await interaction.response.send_message(
                "You need at least 1 militia to conquer land.",
                ephemeral=True,
            )
            return

        if not candidates:
            await interaction.response.send_message(
                "There are no adjacent unclaimed or enemy tiles available.",
                ephemeral=True,
            )
            return

        factions = await faction_manager.list_active_factions(interaction.guild_id)
        faction_names = {f["faction_id"]: f["name"] for f in factions}

        items = [
            {
                "x": tile["x"],
                "y": tile["y"],
                "biome": tile["biome"],
                "owner_name": (
                    faction_names.get(tile["owner_faction_id"], "Unknown")
                    if tile["owner_faction_id"]
                    else "Unclaimed"
                ),
            }
            for tile in candidates
        ]
        items.sort(key=lambda item: (item["owner_name"], item["y"], item["x"]))

        await interaction.response.send_message(
            f"Choose up to **{max_tiles}** frontier tile(s) for **{faction['name']}**. "
            "The limit scales logarithmically with your militia. You will choose the militia force per tile next.",
            view=ConquestView(interaction.user.id, items, max_tiles),
            ephemeral=True,
        )

    @military_group.command(
        name="attack",
        description="Attack another faction, then choose the militia force to commit.",
    )
    async def attack(
        self,
        interaction: discord.Interaction,
        target_faction: str,
    ):
        if not await faction_manager.get_member_faction(
            interaction.guild_id,
            interaction.user.id,
        ):
            await interaction.response.send_message(
                "You must be in a faction to attack.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            AttackMilitiaModal(
                interaction.guild_id,
                interaction.user.id,
                target_faction,
            )
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MilitaryCog(bot))
