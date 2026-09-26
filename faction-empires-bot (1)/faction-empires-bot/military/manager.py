"""
Military actions: turning workers into militia, scouting, conquering
unclaimed/enemy land, and attacking rival factions outright.
"""
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


async def scout(guild_id: int, user_id: int):
    faction = await _require_leader_or_officer(guild_id, user_id)
    await _check_cooldown(faction["faction_id"], "scout", 1.0)

    if faction["militia_count"] < config.SCOUT_MILITIA_COST:
        raise MilitaryError(f"Scouting requires at least {config.SCOUT_MILITIA_COST} militia.")

    lost = 0
    if random.random() < config.SCOUT_LOSS_CHANCE:
        lost = min(config.SCOUT_MILITIA_COST, faction["militia_count"])
        await faction_manager.adjust_militia(faction["faction_id"], -lost)

    candidates = await map_manager.get_adjacent_unclaimed_or_enemy(guild_id, faction["faction_id"])
    intel = []
    owner_cache = {}
    for tile in candidates[:8]:
        owner_name = "Unclaimed"
        if tile["owner_faction_id"]:
            if tile["owner_faction_id"] not in owner_cache:
                f = await faction_manager.get_faction_by_id(tile["owner_faction_id"])
                owner_cache[tile["owner_faction_id"]] = f["name"] if f else "Unknown"
            owner_name = owner_cache[tile["owner_faction_id"]]
        intel.append({"x": tile["x"], "y": tile["y"], "biome": tile["biome"], "owner": owner_name})

    await _set_cooldown(faction["faction_id"], "scout")
    return {"intel": intel, "militia_lost": lost}


async def conquer(guild_id: int, user_id: int, x: int, y: int):
    faction = await _require_leader_or_officer(guild_id, user_id)
    await _check_cooldown(faction["faction_id"], "conquer", config.CONQUER_COOLDOWN_HOURS)

    tile = await db.fetchone("SELECT * FROM tiles WHERE guild_id=? AND x=? AND y=?", (guild_id, x, y))
    if not tile:
        raise MilitaryError("That tile doesn't exist on this map.")
    if tile["biome"] == "ocean":
        raise MilitaryError("You cannot conquer open ocean.")
    if tile["owner_faction_id"] == faction["faction_id"]:
        raise MilitaryError("You already own that tile.")

    adjacent = await map_manager.get_adjacent_unclaimed_or_enemy(guild_id, faction["faction_id"])
    if (x, y) not in {(t["x"], t["y"]) for t in adjacent}:
        raise MilitaryError("You can only conquer land directly adjacent to your existing territory.")

    if faction["militia_count"] < 1:
        raise MilitaryError("You need at least 1 militia to attempt a conquest.")

    defense_bonus = config.TERRAIN_DEFENSE_BONUS.get(tile["biome"], 0.0)
    is_contested = tile["owner_faction_id"] is not None
    defender_militia = 0
    if is_contested:
        defender = await faction_manager.get_faction_by_id(tile["owner_faction_id"])
        defender_militia = defender["militia_count"] if defender else 0

    militia_committed = min(faction["militia_count"], max(1, faction["militia_count"] // 3))
    strength_ratio = militia_committed / (militia_committed + defender_militia + 1)
    success_chance = max(0.05, min(0.95, config.CONQUER_BASE_SUCCESS * strength_ratio * (1 - defense_bonus) + (0.15 if not is_contested else 0)))

    success = random.random() < success_chance
    await _set_cooldown(faction["faction_id"], "conquer")

    result = {"success": success, "chance": success_chance, "tile": {"x": x, "y": y, "biome": tile["biome"]},
              "contested": is_contested}

    if success:
        await db.execute(
            "UPDATE tiles SET owner_faction_id=? WHERE guild_id=? AND x=? AND y=?",
            (faction["faction_id"], guild_id, x, y),
        )
        joined = await faction_manager.claim_wanderers_in_conquered_tiles(guild_id, faction["faction_id"], [(x, y)])
        result["wanderers_joined"] = joined
        # small militia losses even on success
        casualty = random.randint(0, max(1, militia_committed // 4))
        await faction_manager.adjust_militia(faction["faction_id"], -casualty)
        result["militia_lost"] = casualty
        await faction_manager.log_event(
            guild_id, faction["faction_id"], f"Conquered tile ({x},{y}) [{tile['biome']}]."
        )
    else:
        casualty = random.randint(1, max(1, militia_committed // 2))
        await faction_manager.adjust_militia(faction["faction_id"], -casualty)
        result["militia_lost"] = casualty
        await faction_manager.log_event(
            guild_id, faction["faction_id"], f"Failed to conquer tile ({x},{y}), lost {casualty} militia."
        )

    return result


async def attack_faction(guild_id: int, user_id: int, target_faction_name: str, militia_committed: int):
    faction = await _require_leader_or_officer(guild_id, user_id)
    await _check_cooldown(faction["faction_id"], "attack", config.ATTACK_COOLDOWN_HOURS)

    target = await faction_manager.get_faction_by_name(guild_id, target_faction_name)
    if not target:
        raise MilitaryError("No such faction.")
    if target["faction_id"] == faction["faction_id"]:
        raise MilitaryError("You can't attack yourself.")

    # Alliances block attacks
    pact = await db.fetchone(
        "SELECT * FROM diplomacy WHERE guild_id=? AND kind='alliance' AND status='active' AND "
        "((faction_a=? AND faction_b=?) OR (faction_a=? AND faction_b=?))",
        (guild_id, faction["faction_id"], target["faction_id"], target["faction_id"], faction["faction_id"]),
    )
    if pact:
        raise MilitaryError(f"You have an active alliance with **{target['name']}** - break it first with `/diplomacy break`.")

    if militia_committed <= 0 or militia_committed > faction["militia_count"]:
        raise MilitaryError(f"Choose between 1 and {faction['militia_count']} militia to commit.")

    attacker_strength = militia_committed * random.uniform(0.85, 1.15)
    defender_strength = target["militia_count"] * random.uniform(0.85, 1.15) + 2  # small garrison bonus

    attacker_wins = attacker_strength > defender_strength
    await _set_cooldown(faction["faction_id"], "attack")

    result = {"attacker_wins": attacker_wins, "target": target["name"]}

    if attacker_wins:
        attacker_losses = int(militia_committed * random.uniform(0.1, 0.3))
        defender_losses = target["militia_count"]  # defender militia routed
        await faction_manager.adjust_militia(faction["faction_id"], -attacker_losses)
        await faction_manager.adjust_militia(target["faction_id"], -defender_losses)

        # Capture a handful of border tiles from the loser
        target_tiles = await map_manager.get_faction_tiles(guild_id, target["faction_id"])
        n_captured = min(len(target_tiles), random.randint(1, 3))
        captured = random.sample(target_tiles, n_captured) if target_tiles else []
        for t in captured:
            await db.execute(
                "UPDATE tiles SET owner_faction_id=? WHERE guild_id=? AND x=? AND y=?",
                (faction["faction_id"], guild_id, t["x"], t["y"]),
            )
        # Plunder some treasury
        plunder = min(target["treasury"], target["treasury"] * random.uniform(0.05, 0.15))
        await faction_manager.adjust_treasury(target["faction_id"], -plunder)
        await faction_manager.adjust_treasury(faction["faction_id"], plunder)

        result.update({
            "attacker_losses": attacker_losses, "defender_losses": defender_losses,
            "tiles_captured": len(captured), "plunder": plunder,
        })
        await faction_manager.log_event(
            guild_id, faction["faction_id"],
            f"Defeated **{target['name']}** in battle, capturing {len(captured)} tile(s) and {plunder:.1f} money."
        )
        await faction_manager.log_event(
            guild_id, target["faction_id"], f"Was defeated by **{faction['name']}** and lost territory."
        )
    else:
        attacker_losses = int(militia_committed * random.uniform(0.4, 0.7))
        defender_losses = int(target["militia_count"] * random.uniform(0.05, 0.15))
        await faction_manager.adjust_militia(faction["faction_id"], -attacker_losses)
        await faction_manager.adjust_militia(target["faction_id"], -defender_losses)
        result.update({"attacker_losses": attacker_losses, "defender_losses": defender_losses})
        await faction_manager.log_event(
            guild_id, faction["faction_id"], f"Attacked **{target['name']}** and was repelled, losing {attacker_losses} militia."
        )
        await faction_manager.log_event(
            guild_id, target["faction_id"], f"Repelled an attack from **{faction['name']}**."
        )

    return result
