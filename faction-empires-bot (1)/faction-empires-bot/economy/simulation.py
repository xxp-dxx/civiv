"""
The daily "tick" that advances a guild's world by one day:
  1. Each faction produces food & money from its land + workers.
  2. Inhabitants consume food; shortages cause starvation deaths.
  3. Poor factions (low money-per-capita) lose workers to richer factions via migration.
  4. Militia draw upkeep in food & money.
  5. A snapshot of the map is stored ("memory" of the previous map).
  6. There's a config.DISASTER_CHANCE chance of a catastrophe.
  7. Per-faction stats are recorded for leaderboards & the leader dashboard.
"""
import random
from datetime import datetime, timezone

import config
import db
from world import map_manager
from factions import manager as faction_manager


def _now():
    return datetime.now(timezone.utc).isoformat()


async def _production_for_faction(guild_id: int, faction: dict) -> tuple[float, float]:
    """Returns (food_produced, money_produced) for one faction this tick, based on its
    owned tiles' biome modifiers and its worker count."""
    tiles = await map_manager.get_faction_tiles(guild_id, faction["faction_id"])
    if not tiles or faction["worker_count"] <= 0:
        return 0.0, 0.0

    # Workers are spread evenly across owned land for production purposes.
    workers_per_tile = faction["worker_count"] / len(tiles)
    food_total = 0.0
    money_total = 0.0
    for tile in tiles:
        food_mult, money_mult = config.BIOME_MODIFIERS.get(tile["biome"], (1.0, 1.0))
        food_total += workers_per_tile * config.FOOD_PER_WORKER_BASE * food_mult
        money_total += workers_per_tile * config.MONEY_PER_WORKER_BASE * money_mult
    return food_total, money_total


async def _apply_starvation(faction_id: int, food_stock: float) -> int:
    if food_stock >= 0:
        return 0
    shortage = -food_stock
    faction = await faction_manager.get_faction_by_id(faction_id)
    inhabitants = await faction_manager.total_inhabitants(faction)
    deaths = int(min(inhabitants, shortage * config.STARVATION_DEATH_RATE))
    if deaths > 0:
        # Deaths come first from workers, since decisioners are real players.
        workers_lost = min(deaths, faction["worker_count"])
        await faction_manager.adjust_worker_count(faction_id, -workers_lost)
    # Reset food stock to 0 - can't go further negative, debt doesn't carry.
    await db.execute("UPDATE factions SET food_stock=0 WHERE faction_id=?", (faction_id,))
    return deaths


async def _apply_migration(guild_id: int, factions: list[dict]) -> dict[int, int]:
    """Workers emigrate from poor factions to the richest faction in the same guild."""
    migrations = {}
    if len(factions) < 2:
        return migrations

    def money_per_capita(f):
        inhabitants = f["worker_count"] + f["militia_count"] + 1  # +1 avoids div by zero, ignores decisioner count here for speed
        return f["treasury"] / inhabitants

    richest = max(factions, key=money_per_capita)
    for f in factions:
        if f["faction_id"] == richest["faction_id"]:
            continue
        mpc = money_per_capita(f)
        if mpc < config.POVERTY_MONEY_PER_CAPITA_THRESHOLD and f["worker_count"] > 0:
            leaving = max(1, int(f["worker_count"] * config.MIGRATION_FRACTION))
            leaving = min(leaving, f["worker_count"])
            await faction_manager.adjust_worker_count(f["faction_id"], -leaving)
            await faction_manager.adjust_worker_count(richest["faction_id"], leaving)
            migrations[f["faction_id"]] = -leaving
            migrations[richest["faction_id"]] = migrations.get(richest["faction_id"], 0) + leaving
    return migrations


async def run_tick(guild_id: int) -> dict:
    """Advances one guild's world by exactly one day. Returns a summary dict for reporting."""
    cfg = await db.fetchone("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,))
    if not cfg:
        raise ValueError("No world configured for this guild.")

    new_day = cfg["day_count"] + 1
    factions = await faction_manager.list_active_factions(guild_id)

    summary = {"day": new_day, "guild_id": guild_id, "factions": {}, "disaster": None, "migrations": {}}

    # 1 & 4: production + upkeep
    inhabitants_before = {}
    for f in factions:
        inhabitants_before[f["faction_id"]] = await faction_manager.total_inhabitants(f)
        food_prod, money_prod = await _production_for_faction(guild_id, f)
        militia_food_upkeep = f["militia_count"] * config.MILITIA_FOOD_UPKEEP
        militia_money_upkeep = f["militia_count"] * config.MILITIA_UPKEEP_MONEY
        consumption = await faction_manager.total_inhabitants(f) * config.FOOD_CONSUMED_PER_CAPITA

        net_food = food_prod - consumption - militia_food_upkeep
        net_money = money_prod - militia_money_upkeep

        await faction_manager.adjust_food(f["faction_id"], net_food)
        await faction_manager.adjust_treasury(f["faction_id"], net_money)

        summary["factions"][f["faction_id"]] = {
            "name": f["name"], "food_prod": food_prod, "money_prod": money_prod,
            "food_consumed": consumption + militia_food_upkeep, "deaths": 0, "migration": 0,
        }

    # 2: starvation (re-fetch since food_stock just changed)
    for f in factions:
        fresh = await faction_manager.get_faction_by_id(f["faction_id"])
        if fresh is None:
            continue
        deaths = await _apply_starvation(f["faction_id"], fresh["food_stock"])
        summary["factions"][f["faction_id"]]["deaths"] = deaths

    # 3: migration
    fresh_factions = await faction_manager.list_active_factions(guild_id)
    migrations = await _apply_migration(guild_id, fresh_factions)
    summary["migrations"] = migrations
    for fid, delta in migrations.items():
        if fid in summary["factions"]:
            summary["factions"][fid]["migration"] = delta

    # 5: disaster
    disaster = await map_manager.maybe_trigger_disaster(guild_id, new_day)
    summary["disaster"] = disaster

    # 6: stats history + inhabitants delta
    final_factions = await faction_manager.list_active_factions(guild_id)
    for f in final_factions:
        land_count = await map_manager.count_faction_land(guild_id, f["faction_id"])
        inhabitants_now = await faction_manager.total_inhabitants(f)
        before = inhabitants_before.get(f["faction_id"], inhabitants_now)
        stat = summary["factions"].get(f["faction_id"], {"food_prod": 0, "money_prod": 0, "food_consumed": 0})
        await db.execute(
            "INSERT INTO stats_history (guild_id, faction_id, day, food_prod, money_prod, food_consumed, "
            "inhabitants, inhabitants_delta, land_count) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(faction_id, day) DO UPDATE SET food_prod=excluded.food_prod, money_prod=excluded.money_prod, "
            "food_consumed=excluded.food_consumed, inhabitants=excluded.inhabitants, "
            "inhabitants_delta=excluded.inhabitants_delta, land_count=excluded.land_count",
            (guild_id, f["faction_id"], new_day, stat["food_prod"], stat["money_prod"], stat["food_consumed"],
             inhabitants_now, inhabitants_now - before, land_count),
        )

    # 7: snapshot + advance day counter
    await map_manager.snapshot_map(guild_id, new_day)
    await db.execute(
        "UPDATE guild_config SET day_count=?, last_tick_at=? WHERE guild_id=?", (new_day, _now(), guild_id)
    )

    return summary


async def get_faction_dashboard(faction_id: int, days: int = 7):
    """Leader-facing analytics: recent production per capita and inhabitant gain/loss."""
    faction = await faction_manager.get_faction_by_id(faction_id)
    if not faction:
        return None
    history = await db.fetchall(
        "SELECT * FROM stats_history WHERE faction_id=? ORDER BY day DESC LIMIT ?", (faction_id, days)
    )
    inhabitants = await faction_manager.total_inhabitants(faction)
    land = await map_manager.count_faction_land(faction["guild_id"], faction_id)
    latest = history[0] if history else None
    return {
        "faction": faction,
        "inhabitants": inhabitants,
        "land": land,
        "food_per_capita": (latest["food_prod"] / inhabitants) if latest and inhabitants else 0.0,
        "money_per_capita": (latest["money_prod"] / inhabitants) if latest and inhabitants else 0.0,
        "history": history,
    }
