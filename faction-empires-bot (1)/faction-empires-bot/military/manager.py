"""Military actions: militia conversion, territorial conquest, and direct faction attacks."""
import random
from datetime import datetime, timedelta, timezone

import config
import db
from world import map_manager
from factions import manager as faction_manager


class MilitaryError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


async def _check_cooldown(faction_id: int, action: str, hours: float):
    row = await db.fetchone(
        "SELECT last_used_at FROM cooldowns WHERE faction_id=? AND action=?", (faction_id, action)
    )
    if row:
        last = datetime.fromisoformat(row["last_used_at"])
        elapsed = _now() - last
        if elapsed < timedelta(hours=hours):
            remaining = timedelta(hours=hours) - elapsed
            mins = int(remaining.total_seconds() // 60)
            raise MilitaryError(f"That action is on cooldown for another {mins} minute(s).")


async def _set_cooldown(faction_id: int, action: str):
    await db.execute(
        "INSERT INTO cooldowns (guild_id, faction_id, action, last_used_at) VALUES ("
        "(SELECT guild_id FROM factions WHERE faction_id=?), ?, ?, ?) "
        "ON CONFLICT(faction_id, action) DO UPDATE SET last_used_at=excluded.last_used_at",
        (faction_id, faction_id, action, _now().isoformat()),
    )


async def _require_leader_or_officer(guild_id: int, user_id: int):
    faction = await faction_manager.get_member_faction(guild_id, user_id)
    if not faction:
        raise MilitaryError("You must be in a faction to do that.")
    member = await faction_manager.get_member(guild_id, user_id)
    if member["role"] not in ("leader", "officer"):
        raise MilitaryError("Only the faction leader or an officer can issue military orders.")
    return faction


async def convert_to_militia(guild_id: int, user_id: int, amount: int):
    faction = await _require_leader_or_officer(guild_id, user_id)
    if amount <= 0:
        raise MilitaryError("Choose a positive number of workers to convert.")
    if amount > faction["worker_count"]:
        raise MilitaryError(f"Your faction only has {faction['worker_count']} workers available.")
    await faction_manager.adjust_worker_count(faction["faction_id"], -amount)
    await faction_manager.adjust_militia(faction["faction_id"], amount)
    await faction_manager.log_event(
        guild_id, faction["faction_id"], f"{amount} workers were conscripted into the militia."
    )
    return await faction_manager.get_faction_by_id(faction["faction_id"])


def get_conquest_tile_limit(militia_count: int) -> int:
    """Return the maximum frontier tiles that can be targeted in one conquest cooldown.

    The progression is logarithmic: 1 tile below 100 militia, then each additional
    tile requires roughly twice as much militia, capped at 10 tiles.
    """
    import math

    if militia_count <= 0:
        return 0
    extra_tiers = max(0, int(math.log2(militia_count / config.CONQUER_MILITIA_SCALE)))
    return min(
        config.CONQUER_MAX_TILES_PER_COOLDOWN,
        max(config.CONQUER_MIN_TILES_PER_COOLDOWN, 1 + extra_tiers),
    )


async def _resolve_conquest_tile(
    guild_id: int,
    faction: dict,
    tile: dict,
    militia_committed: int,
):
    defense_bonus = config.TERRAIN_DEFENSE_BONUS.get(tile["biome"], 0.0)
    is_contested = tile["owner_faction_id"] is not None
    defender_militia = 0
    if is_contested:
        defender = await faction_manager.get_faction_by_id(tile["owner_faction_id"])
        defender_militia = defender["militia_count"] if defender else 0

    strength_ratio = militia_committed / (militia_committed + defender_militia + 1)
    success_chance = max(
        0.05,
        min(
            0.95,
            config.CONQUER_BASE_SUCCESS * strength_ratio * (1 - defense_bonus)
            + (0.15 if not is_contested else 0),
        ),
    )
    success = random.random() < success_chance

    result = {
        "success": success,
        "chance": success_chance,
        "tile": {"x": tile["x"], "y": tile["y"], "biome": tile["biome"]},
        "contested": is_contested,
        "militia_committed": militia_committed,
        "militia_lost": 0,
        "wanderers_joined": [],
    }

    if success:
        await db.execute(
            "UPDATE tiles SET owner_faction_id=? WHERE guild_id=? AND x=? AND y=?",
            (faction["faction_id"], guild_id, tile["x"], tile["y"]),
        )
        joined = await faction_manager.claim_wanderers_in_conquered_tiles(
            guild_id, faction["faction_id"], [(tile["x"], tile["y"])]
        )
        result["wanderers_joined"] = joined
        casualty = random.randint(0, max(1, militia_committed // 4))
        await faction_manager.adjust_militia(faction["faction_id"], -casualty)
        result["militia_lost"] = casualty
        await faction_manager.log_event(
            guild_id,
            faction["faction_id"],
            f"Conquered tile ({tile['x']},{tile['y']}) [{tile['biome']}] with "
            f"{militia_committed} militia committed.",
        )
    else:
        casualty = random.randint(1, max(1, militia_committed // 2))
        await faction_manager.adjust_militia(faction["faction_id"], -casualty)
        result["militia_lost"] = casualty
        await faction_manager.log_event(
            guild_id,
            faction["faction_id"],
            f"Failed to conquer tile ({tile['x']},{tile['y']}), losing "
            f"{casualty} of {militia_committed} committed militia.",
        )
    return result


async def conquer_multiple(
    guild_id: int,
    user_id: int,
    targets: list[tuple[int, int]],
    militia_per_tile: int,
):
    """Conquer several selected adjacent tiles in one cooldown window."""
    faction = await _require_leader_or_officer(guild_id, user_id)
    await _check_cooldown(
        faction["faction_id"], "conquer", config.CONQUER_COOLDOWN_HOURS
    )

    unique_targets = list(dict.fromkeys(targets))
    if not unique_targets:
        raise MilitaryError("Choose at least one frontier tile.")

    max_tiles = get_conquest_tile_limit(faction["militia_count"])
    if len(unique_targets) > max_tiles:
        raise MilitaryError(
            f"Your {faction['militia_count']} militia currently allows up to "
            f"{max_tiles} tile(s) per conquest cooldown."
        )

    if militia_per_tile <= 0:
        raise MilitaryError("Choose a positive number of militia per tile.")

    total_required = militia_per_tile * len(unique_targets)
    if total_required > faction["militia_count"]:
        raise MilitaryError(
            f"That force would require {total_required} militia, but you only have "
            f"{faction['militia_count']}."
        )

    adjacent = await map_manager.get_adjacent_unclaimed_or_enemy(
        guild_id, faction["faction_id"]
    )
    adjacent_by_xy = {(t["x"], t["y"]): t for t in adjacent}

    tiles = []
    for x, y in unique_targets:
        tile = await db.fetchone(
            "SELECT * FROM tiles WHERE guild_id=? AND x=? AND y=?",
            (guild_id, x, y),
        )
        if not tile:
            raise MilitaryError(f"Tile ({x},{y}) does not exist on this map.")
        if tile["biome"] == "ocean":
            raise MilitaryError(f"Tile ({x},{y}) is open ocean.")
        if tile["owner_faction_id"] == faction["faction_id"]:
            raise MilitaryError(f"You already own tile ({x},{y}).")
        if (x, y) not in adjacent_by_xy:
            raise MilitaryError(
                f"Tile ({x},{y}) is not directly adjacent to your territory."
            )
        tiles.append(tile)

    await _set_cooldown(faction["faction_id"], "conquer")

    return [
        await _resolve_conquest_tile(
            guild_id, faction, tile, militia_per_tile
        )
        for tile in tiles
    ]


async def conquer(guild_id: int, user_id: int, x: int, y: int, militia_committed: int):
    """Backward-compatible single-tile conquest wrapper."""
    results = await conquer_multiple(
        guild_id,
        user_id,
        [(x, y)],
        militia_committed,
    )
    return results[0]

async def attack_faction(guild_id: int, user_id: int, target_faction_name: str, militia_committed: int):
    faction = await _require_leader_or_officer(guild_id, user_id)
    await _check_cooldown(faction["faction_id"], "attack", config.ATTACK_COOLDOWN_HOURS)
    target = await faction_manager.get_faction_by_name(guild_id, target_faction_name)
    if not target:
        raise MilitaryError("No such faction.")
    if target["faction_id"] == faction["faction_id"]:
        raise MilitaryError("You can't attack yourself.")

    pact = await db.fetchone(
        "SELECT * FROM diplomacy WHERE guild_id=? AND kind='alliance' AND status='active' AND "
        "((faction_a=? AND faction_b=?) OR (faction_a=? AND faction_b=?))",
        (guild_id, faction["faction_id"], target["faction_id"], target["faction_id"], faction["faction_id"]),
    )
    if pact:
        raise MilitaryError(
            f"You have an active alliance with {target['name']}. Break it before attacking."
        )
    if militia_committed <= 0 or militia_committed > faction["militia_count"]:
        raise MilitaryError(f"Choose between 1 and {faction['militia_count']} militia to commit.")

    attacker_strength = militia_committed * random.uniform(0.85, 1.15)
    defender_strength = target["militia_count"] * random.uniform(0.85, 1.15) + 2
    attacker_wins = attacker_strength > defender_strength
    await _set_cooldown(faction["faction_id"], "attack")
    result = {"attacker_wins": attacker_wins, "target": target["name"]}

    if attacker_wins:
        attacker_losses = int(militia_committed * random.uniform(0.1, 0.3))
        defender_losses = target["militia_count"]
        await faction_manager.adjust_militia(faction["faction_id"], -attacker_losses)
        await faction_manager.adjust_militia(target["faction_id"], -defender_losses)
        target_tiles = await map_manager.get_faction_tiles(guild_id, target["faction_id"])
        n_captured = min(len(target_tiles), random.randint(1, 3))
        captured = random.sample(target_tiles, n_captured) if target_tiles else []
        for t in captured:
            await db.execute(
                "UPDATE tiles SET owner_faction_id=? WHERE guild_id=? AND x=? AND y=?",
                (faction["faction_id"], guild_id, t["x"], t["y"]),
            )
        plunder = min(target["treasury"], target["treasury"] * random.uniform(0.05, 0.15))
        await faction_manager.adjust_treasury(target["faction_id"], -plunder)
        await faction_manager.adjust_treasury(faction["faction_id"], plunder)
        result.update({
            "attacker_losses": attacker_losses,
            "defender_losses": defender_losses,
            "tiles_captured": len(captured),
            "plunder": plunder,
        })
        await faction_manager.log_event(
            guild_id, faction["faction_id"],
            f"Defeated {target['name']} in battle, capturing {len(captured)} tile(s) and {plunder:.1f} money.",
        )
        await faction_manager.log_event(
            guild_id, target["faction_id"], f"Was defeated by {faction['name']} and lost territory."
        )
    else:
        attacker_losses = int(militia_committed * random.uniform(0.4, 0.7))
        defender_losses = int(target["militia_count"] * random.uniform(0.05, 0.15))
        await faction_manager.adjust_militia(faction["faction_id"], -attacker_losses)
        await faction_manager.adjust_militia(target["faction_id"], -defender_losses)
        result.update({
            "attacker_losses": attacker_losses,
            "defender_losses": defender_losses,
        })
        await faction_manager.log_event(
            guild_id, faction["faction_id"],
            f"Attacked {target['name']} and was repelled, losing {attacker_losses} militia.",
        )
        await faction_manager.log_event(
            guild_id, target["faction_id"],
            f"Repelled an attack from {faction['name']}.",
        )
    return result
