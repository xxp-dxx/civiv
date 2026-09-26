"""
Leaderboard queries. "Server" leaderboards are scoped to one guild_id;
"global" leaderboards rank factions across every guild the bot is active in.

Metrics: food production, money production, inhabitants, land owned.
Production metrics use each faction's most recent stats_history entry
(i.e. yesterday's tick) since that's the last *rate* we actually measured;
inhabitants/land are live current values.
"""
import db
from factions import manager as faction_manager
from world import map_manager

METRICS = ("food", "money", "inhabitants", "land")


async def _latest_stats_by_faction(guild_id: int | None = None):
    """Returns {faction_id: latest stats_history row} - optionally scoped to one guild."""
    if guild_id is not None:
        rows = await db.fetchall(
            "SELECT s.* FROM stats_history s "
            "INNER JOIN (SELECT faction_id, MAX(day) d FROM stats_history WHERE guild_id=? GROUP BY faction_id) m "
            "ON s.faction_id = m.faction_id AND s.day = m.d WHERE s.guild_id=?",
            (guild_id, guild_id),
        )
    else:
        rows = await db.fetchall(
            "SELECT s.* FROM stats_history s "
            "INNER JOIN (SELECT faction_id, MAX(day) d FROM stats_history GROUP BY faction_id) m "
            "ON s.faction_id = m.faction_id AND s.day = m.d"
        )
    return {r["faction_id"]: r for r in rows}


async def build_leaderboard(metric: str, guild_id: int | None = None, limit: int = 10):
    """metric in METRICS. guild_id=None means global (cross-server)."""
    if metric not in METRICS:
        raise ValueError(f"Unknown metric '{metric}'")

    if guild_id is not None:
        factions = await faction_manager.list_active_factions(guild_id)
    else:
        factions = await db.fetchall("SELECT * FROM factions WHERE is_active=1")

    stats = await _latest_stats_by_faction(guild_id)

    entries = []
    for f in factions:
        s = stats.get(f["faction_id"])
        if metric == "food":
            value = s["food_prod"] if s else 0.0
        elif metric == "money":
            value = s["money_prod"] if s else 0.0
        elif metric == "inhabitants":
            value = await faction_manager.total_inhabitants(f)
        elif metric == "land":
            value = await map_manager.count_faction_land(f["guild_id"], f["faction_id"])
        entries.append({"faction": f, "value": value})

    entries.sort(key=lambda e: e["value"], reverse=True)
    return entries[:limit]
