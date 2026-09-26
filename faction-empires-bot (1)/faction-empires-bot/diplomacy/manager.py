"""
Diplomacy: proposing/accepting/breaking alliances and trade pacts between factions.

Alliances: purely protective (blocks attacks between the two factions - see military/manager.py).
Trade pacts: each tick, allied-by-trade factions automatically share a small
percentage of food/money surplus with their poorer partner (implemented in
economy hooks below, invoked from the tick if desired).
"""
from datetime import datetime, timezone

import db
from factions import manager as faction_manager


class DiplomacyError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


async def _require_leader(guild_id: int, user_id: int):
    faction = await faction_manager.get_member_faction(guild_id, user_id)
    if not faction:
        raise DiplomacyError("You must be in a faction to conduct diplomacy.")
    if faction["leader_id"] != user_id:
        raise DiplomacyError("Only the faction leader can conduct diplomacy.")
    return faction


async def propose(guild_id: int, user_id: int, target_faction_name: str, kind: str):
    assert kind in ("alliance", "trade")
    faction = await _require_leader(guild_id, user_id)
    target = await faction_manager.get_faction_by_name(guild_id, target_faction_name)
    if not target:
        raise DiplomacyError("No such faction.")
    if target["faction_id"] == faction["faction_id"]:
        raise DiplomacyError("You can't propose diplomacy with yourself.")

    existing = await db.fetchone(
        "SELECT * FROM diplomacy WHERE guild_id=? AND kind=? AND status IN ('pending','active') AND "
        "((faction_a=? AND faction_b=?) OR (faction_a=? AND faction_b=?))",
        (guild_id, kind, faction["faction_id"], target["faction_id"], target["faction_id"], faction["faction_id"]),
    )
    if existing:
        raise DiplomacyError(f"There's already a pending or active {kind} between your factions.")

    cur = await db.execute(
        "INSERT INTO diplomacy (guild_id, faction_a, faction_b, kind, status, created_at) VALUES (?,?,?,?,?,?)",
        (guild_id, faction["faction_id"], target["faction_id"], kind, "pending", _now()),
    )
    await faction_manager.log_event(
        guild_id, target["faction_id"], f"**{faction['name']}** proposed a {kind} with your faction. Use `/diplomacy accept`."
    )
    return cur.lastrowid


async def accept(guild_id: int, user_id: int, from_faction_name: str, kind: str):
    faction = await _require_leader(guild_id, user_id)
    proposer = await faction_manager.get_faction_by_name(guild_id, from_faction_name)
    if not proposer:
        raise DiplomacyError("No such faction.")

    row = await db.fetchone(
        "SELECT * FROM diplomacy WHERE guild_id=? AND kind=? AND status='pending' AND faction_a=? AND faction_b=?",
        (guild_id, kind, proposer["faction_id"], faction["faction_id"]),
    )
    if not row:
        raise DiplomacyError("No pending proposal found from that faction.")

    await db.execute("UPDATE diplomacy SET status='active' WHERE id=?", (row["id"],))
    await faction_manager.log_event(
        guild_id, proposer["faction_id"], f"**{faction['name']}** accepted your {kind} proposal."
    )
    await faction_manager.log_event(
        guild_id, faction["faction_id"], f"You are now in a {kind} with **{proposer['name']}**."
    )
    return True


async def break_pact(guild_id: int, user_id: int, other_faction_name: str, kind: str | None = None):
    faction = await _require_leader(guild_id, user_id)
    other = await faction_manager.get_faction_by_name(guild_id, other_faction_name)
    if not other:
        raise DiplomacyError("No such faction.")

    query = (
        "SELECT * FROM diplomacy WHERE guild_id=? AND status='active' AND "
        "((faction_a=? AND faction_b=?) OR (faction_a=? AND faction_b=?))"
    )
    params = [guild_id, faction["faction_id"], other["faction_id"], other["faction_id"], faction["faction_id"]]
    if kind:
        query += " AND kind=?"
        params.append(kind)
    rows = await db.fetchall(query, tuple(params))
    if not rows:
        raise DiplomacyError("No active diplomatic relationship with that faction.")

    for row in rows:
        await db.execute("UPDATE diplomacy SET status='broken' WHERE id=?", (row["id"],))
    await faction_manager.log_event(
        guild_id, other["faction_id"], f"**{faction['name']}** has broken diplomatic ties with your faction."
    )
    return len(rows)


async def list_relations(guild_id: int, faction_id: int):
    return await db.fetchall(
        "SELECT * FROM diplomacy WHERE guild_id=? AND status IN ('active','pending') AND "
        "(faction_a=? OR faction_b=?) ORDER BY id DESC",
        (guild_id, faction_id, faction_id),
    )


async def are_allied(guild_id: int, faction_a: int, faction_b: int) -> bool:
    row = await db.fetchone(
        "SELECT 1 FROM diplomacy WHERE guild_id=? AND kind='alliance' AND status='active' AND "
        "((faction_a=? AND faction_b=?) OR (faction_a=? AND faction_b=?))",
        (guild_id, faction_a, faction_b, faction_b, faction_a),
    )
    return row is not None


async def apply_trade_sharing(guild_id: int):
    """Optional tick hook: active trade-pact partners share a slice of surplus resources.
    Called from the economy tick if you want trade pacts to have a mechanical effect."""
    rows = await db.fetchall(
        "SELECT * FROM diplomacy WHERE guild_id=? AND kind='trade' AND status='active'", (guild_id,)
    )
    for row in rows:
        fa = await faction_manager.get_faction_by_id(row["faction_a"])
        fb = await faction_manager.get_faction_by_id(row["faction_b"])
        if not fa or not fb:
            continue
        # richer partner gives 5% of the money gap to the poorer one; same for food.
        money_gap = fa["treasury"] - fb["treasury"]
        food_gap = fa["food_stock"] - fb["food_stock"]
        share_rate = 0.05
        if abs(money_gap) > 1:
            amt = money_gap * share_rate
            await faction_manager.adjust_treasury(fa["faction_id"], -amt)
            await faction_manager.adjust_treasury(fb["faction_id"], amt)
        if abs(food_gap) > 1:
            amt = food_gap * share_rate
            await faction_manager.adjust_food(fa["faction_id"], -amt)
            await faction_manager.adjust_food(fb["faction_id"], amt)
