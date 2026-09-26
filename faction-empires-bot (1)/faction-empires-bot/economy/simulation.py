"""Daily simulation for Faction Empires."""
import random
from datetime import datetime, timezone

import config
import db
from world import map_manager
from factions import manager as faction_manager


def _now():
    return datetime.now(timezone.utc).isoformat()


def _stochastic_count(expected: float) -> int:
    if expected <= 0:
        return 0
    whole = int(expected)
    if random.random() < expected - whole:
        whole += 1
    return whole


def _growth_rate(net_food: float, food_consumed: float, food_stock: float) -> float:
    """Return natural worker growth from food security and current reserves.

    Daily surplus controls the growth curve, but positive reserves provide a
    baseline level of food security even when production only breaks even or
    runs a temporary deficit. Growth falls to zero when reserves are exhausted.
    """
    if food_consumed <= 0 or food_stock <= 0:
        return 0.0

    reserve_factor = min(1.0, food_stock / (food_consumed * config.NATURAL_GROWTH_RESERVE_DAYS))
    normalized_balance = net_food / food_consumed
    surplus_score = min(
        1.0,
        max(0.0, (normalized_balance + config.NATURAL_GROWTH_DEFICIT_TOLERANCE)
                / (1.0 + config.NATURAL_GROWTH_DEFICIT_TOLERANCE)),
    )
    curved_surplus = surplus_score ** config.NATURAL_GROWTH_CURVE_EXPONENT
    growth_pressure = (
        config.NATURAL_GROWTH_BASE_FACTOR
        + (1.0 - config.NATURAL_GROWTH_BASE_FACTOR) * curved_surplus
    )
    return config.NATURAL_GROWTH_MAX_RATE * reserve_factor * growth_pressure


async def _production_for_faction(guild_id: int, faction: dict) -> tuple[float, float]:
    tiles = await map_manager.get_faction_tiles(guild_id, faction["faction_id"])
    if not tiles or faction["worker_count"] <= 0:
        return 0.0, 0.0
    workers_per_tile = faction["worker_count"] / len(tiles)
    food_total = 0.0
    money_total = 0.0
    for tile in tiles:
        food_mult, money_mult = config.BIOME_MODIFIERS.get(tile["biome"], (1.0, 1.0))
        food_total += workers_per_tile * config.FOOD_PER_WORKER_BASE * food_mult
        money_total += workers_per_tile * config.MONEY_PER_WORKER_BASE * money_mult
    return food_total, money_total


async def get_faction_economy(faction: dict) -> dict:
    tiles = await map_manager.get_faction_tiles(faction["guild_id"], faction["faction_id"])
    row = await db.fetchone("SELECT COUNT(*) c FROM members WHERE faction_id=?", (faction["faction_id"],))
    decisioners = row["c"] if row else 0
    inhabitants = faction["worker_count"] + faction["militia_count"] + decisioners

    food_prod = 0.0
    money_prod = 0.0
    if tiles and faction["worker_count"] > 0:
        workers_per_tile = faction["worker_count"] / len(tiles)
        for tile in tiles:
            food_mult, money_mult = config.BIOME_MODIFIERS.get(tile["biome"], (1.0, 1.0))
            food_prod += workers_per_tile * config.FOOD_PER_WORKER_BASE * food_mult
            money_prod += workers_per_tile * config.MONEY_PER_WORKER_BASE * money_mult

    food_consumption = inhabitants * config.FOOD_CONSUMED_PER_CAPITA
    militia_food_upkeep = faction["militia_count"] * config.MILITIA_FOOD_UPKEEP
    total_food_consumed = food_consumption + militia_food_upkeep
    net_food = food_prod - total_food_consumed
    militia_money_upkeep = faction["militia_count"] * config.MILITIA_UPKEEP_MONEY
    net_money = money_prod - militia_money_upkeep
    growth_rate = _growth_rate(net_food, total_food_consumed, faction["food_stock"])

    return {
        "decisioners": decisioners,
        "inhabitants": inhabitants,
        "food_prod": food_prod,
        "money_prod": money_prod,
        "food_consumption": food_consumption,
        "militia_food_upkeep": militia_food_upkeep,
        "total_food_consumed": total_food_consumed,
        "net_food": net_food,
        "militia_money_upkeep": militia_money_upkeep,
        "net_money": net_money,
        "growth_rate": growth_rate,
        "growth_percent": growth_rate * 100.0,
        "crisis": faction["worker_count"] <= 0 and inhabitants > 0,
        "food_days": (
            faction["food_stock"] / total_food_consumed if total_food_consumed > 0 else float("inf")
        ),
    }


async def _apply_starvation(faction_id: int, food_stock: float) -> int:
    if food_stock >= 0:
        return 0
    shortage = -food_stock
    faction = await faction_manager.get_faction_by_id(faction_id)
    inhabitants = await faction_manager.total_inhabitants(faction)
    deaths = int(min(inhabitants, shortage * config.STARVATION_DEATH_RATE))
    if deaths > 0:
        workers_lost = min(deaths, faction["worker_count"])
        if workers_lost:
            await faction_manager.adjust_worker_count(faction_id, -workers_lost)
    await db.execute("UPDATE factions SET food_stock=0 WHERE faction_id=?", (faction_id,))
    return deaths


async def _apply_natural_population(faction_id: int) -> tuple[int, int, float]:
    faction = await faction_manager.get_faction_by_id(faction_id)
    if not faction or faction["worker_count"] <= 0:
        return 0, 0, 0.0
    economy = await get_faction_economy(faction)
    births = _stochastic_count(faction["worker_count"] * economy["growth_rate"])
    deaths = _stochastic_count(faction["worker_count"] * config.NATURAL_DEATH_RATE)
    if births:
        await faction_manager.adjust_worker_count(faction_id, births)
    if deaths:
        await faction_manager.adjust_worker_count(faction_id, -deaths)
    return births, deaths, economy["growth_rate"]


async def _apply_migration(guild_id: int, factions: list[dict]) -> dict[int, int]:
    migrations = {}
    if len(factions) < 2:
        return migrations

    def money_per_capita(f):
        inhabitants = f["worker_count"] + f["militia_count"] + 1
        return f["treasury"] / inhabitants

    richest = max(factions, key=money_per_capita)
    for f in factions:
        if f["faction_id"] == richest["faction_id"]:
            continue
        if money_per_capita(f) < config.POVERTY_MONEY_PER_CAPITA_THRESHOLD and f["worker_count"] > 0:
            leaving = max(1, int(f["worker_count"] * config.MIGRATION_FRACTION))
            leaving = min(leaving, f["worker_count"])
            await faction_manager.adjust_worker_count(f["faction_id"], -leaving)
            await faction_manager.adjust_worker_count(richest["faction_id"], leaving)
            migrations[f["faction_id"]] = -leaving
            migrations[richest["faction_id"]] = migrations.get(richest["faction_id"], 0) + leaving
    return migrations


async def run_tick(guild_id: int) -> dict:
    cfg = await db.fetchone("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,))
    if not cfg:
        raise ValueError("No world configured for this guild.")

    new_day = cfg["day_count"] + 1
    factions = await faction_manager.list_active_factions(guild_id)
    summary = {"day": new_day, "guild_id": guild_id, "factions": {}, "disaster": None, "migrations": {}}
    inhabitants_before = {}

    for f in factions:
        inhabitants_before[f["faction_id"]] = await faction_manager.total_inhabitants(f)
        economy = await get_faction_economy(f)
        await faction_manager.adjust_food(f["faction_id"], economy["net_food"])
        await faction_manager.adjust_treasury(f["faction_id"], economy["net_money"])
        summary["factions"][f["faction_id"]] = {
            "name": f["name"],
            "food_prod": economy["food_prod"],
            "money_prod": economy["money_prod"],
            "food_consumed": economy["total_food_consumed"],
            "net_food": economy["net_food"],
            "food_stock": f["food_stock"] + economy["net_food"],
            "deaths": 0,
            "natural_deaths": 0,
            "births": 0,
            "migration": 0,
            "growth_rate": economy["growth_rate"],
            "crisis": False,
        }

    for f in factions:
        fresh = await faction_manager.get_faction_by_id(f["faction_id"])
        if fresh is None:
            continue
        summary["factions"][f["faction_id"]]["deaths"] += await _apply_starvation(
            f["faction_id"], fresh["food_stock"]
        )

    for f in factions:
        if await faction_manager.get_faction_by_id(f["faction_id"]) is None:
            continue
        births, natural_deaths, growth_rate = await _apply_natural_population(f["faction_id"])
        data = summary["factions"][f["faction_id"]]
        data["births"] = births
        data["natural_deaths"] = natural_deaths
        data["deaths"] += natural_deaths
        data["growth_rate"] = growth_rate

    fresh_factions = await faction_manager.list_active_factions(guild_id)
    migrations = await _apply_migration(guild_id, fresh_factions)
    summary["migrations"] = migrations
    for fid, delta in migrations.items():
        if fid in summary["factions"]:
            summary["factions"][fid]["migration"] = delta

    summary["disaster"] = await map_manager.maybe_trigger_disaster(guild_id, new_day)

    final_factions = await faction_manager.list_active_factions(guild_id)
    for f in final_factions:
        land_count = await map_manager.count_faction_land(guild_id, f["faction_id"])
        inhabitants_now = await faction_manager.total_inhabitants(f)
        before = inhabitants_before.get(f["faction_id"], inhabitants_now)
        stat = summary["factions"].get(f["faction_id"], {"food_prod": 0, "money_prod": 0, "food_consumed": 0})
        economy = await get_faction_economy(f)
        stat["food_stock"] = f["food_stock"]
        stat["net_food"] = economy["net_food"]
        stat["crisis"] = economy["crisis"]
        await db.execute(
            "INSERT INTO stats_history (guild_id, faction_id, day, food_prod, money_prod, food_consumed, "
            "inhabitants, inhabitants_delta, land_count) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(faction_id, day) DO UPDATE SET food_prod=excluded.food_prod, money_prod=excluded.money_prod, "
            "food_consumed=excluded.food_consumed, inhabitants=excluded.inhabitants, "
            "inhabitants_delta=excluded.inhabitants_delta, land_count=excluded.land_count",
            (guild_id, f["faction_id"], new_day, stat["food_prod"], stat["money_prod"], stat["food_consumed"],
             inhabitants_now, inhabitants_now - before, land_count),
        )

    await map_manager.snapshot_map(guild_id, new_day)
    await db.execute(
        "UPDATE guild_config SET day_count=?, last_tick_at=? WHERE guild_id=?",
        (new_day, _now(), guild_id)
    )
    return summary


async def get_faction_dashboard(faction_id: int, days: int = 7):
    faction = await faction_manager.get_faction_by_id(faction_id)
    if not faction:
        return None
    history = await db.fetchall(
        "SELECT * FROM stats_history WHERE faction_id=? ORDER BY day DESC LIMIT ?",
        (faction_id, days)
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
