"""
Everything about factions themselves: creation, membership (decisioners),
leadership, merging one faction into another, and "wanderers" (players
who belong to no faction and sit at a random point on the map).

Terminology recap:
 - decisioners: real Discord members who belong to a faction (members table, faction_id set)
 - workers: simulated NPC population, stored as a single integer per faction (worker_count)
 - inhabitants: decisioners + workers (+ militia, who are ex-workers under arms)
"""
import random
from datetime import datetime, timezone

import config
import db
from world import map_manager


class FactionError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Color validation
# --------------------------------------------------------------------------

def _hex_to_rgb(hex_color: str):
    hex_color = hex_color.strip().lstrip("#")
    if len(hex_color) != 6 or any(c not in "0123456789abcdefABCDEF" for c in hex_color):
        raise FactionError(f"`{hex_color}` isn't a valid hex color. Use a format like `#3498DB`.")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def _redmean_distance(c1, c2) -> float:
    """Perceptually-weighted color distance ('redmean'), better than raw Euclidean RGB distance."""
    r1, g1, b1 = c1
    r2, g2, b2 = c2
    rmean = (r1 + r2) / 2
    dr, dg, db_ = r1 - r2, g1 - g2, b1 - b2
    return ((2 + rmean / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rmean) / 256) * db_ * db_) ** 0.5


async def validate_color(guild_id: int, hex_color: str):
    rgb = _hex_to_rgb(hex_color)
    existing = await db.fetchall("SELECT color_hex FROM factions WHERE guild_id=? AND is_active=1", (guild_id,))
    for row in existing:
        other_rgb = _hex_to_rgb(row["color_hex"])
        dist = _redmean_distance(rgb, other_rgb)
        if dist < config.MIN_COLOR_DISTANCE:
            raise FactionError(
                f"That color is too close to an existing faction's color (`{row['color_hex']}`). "
                "Please pick a more distinct hue."
            )
    return "#" + "".join(f"{v:02X}" for v in rgb)


# --------------------------------------------------------------------------
# Member / wanderer lookups
# --------------------------------------------------------------------------

async def ensure_member_row(guild_id: int, user_id: int):
    """Every player who ever interacts with the bot gets a row. New players are wanderers."""
    row = await db.fetchone("SELECT * FROM members WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    if row:
        return row
    cfg = await db.fetchone("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,))
    if not cfg:
        raise FactionError("This server doesn't have a world yet - ask an admin to run `/admin init_world`.")
    wx = random.randint(0, cfg["map_width"] - 1)
    wy = random.randint(0, cfg["map_height"] - 1)
    await db.execute(
        "INSERT INTO members (guild_id, user_id, faction_id, role, wander_x, wander_y, joined_at) "
        "VALUES (?, ?, NULL, 'wanderer', ?, ?, ?)",
        (guild_id, user_id, wx, wy, _now()),
    )
    return await db.fetchone("SELECT * FROM members WHERE guild_id=? AND user_id=?", (guild_id, user_id))


async def get_member(guild_id: int, user_id: int):
    return await ensure_member_row(guild_id, user_id)


async def get_faction_by_name(guild_id: int, name: str):
    return await db.fetchone(
        "SELECT * FROM factions WHERE guild_id=? AND lower(name)=lower(?) AND is_active=1", (guild_id, name)
    )


async def get_faction_by_id(faction_id: int):
    return await db.fetchone("SELECT * FROM factions WHERE faction_id=? AND is_active=1", (faction_id,))


async def get_member_faction(guild_id: int, user_id: int):
    member = await get_member(guild_id, user_id)
    if not member["faction_id"]:
        return None
    return await get_faction_by_id(member["faction_id"])


async def list_faction_members(faction_id: int):
    return await db.fetchall("SELECT * FROM members WHERE faction_id=?", (faction_id,))


async def move_wanderer(guild_id: int, user_id: int, direction: str):
    offsets = {"north": (0, -1), "south": (0, 1), "west": (-1, 0), "east": (1, 0)}
    member = await get_member(guild_id, user_id)
    if member["faction_id"]:
        raise FactionError("Only wanderers can move this way.")
    if direction not in offsets:
        raise FactionError("Direction must be north, south, east, or west.")

    cfg = await db.fetchone("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,))
    dx, dy = offsets[direction]
    nx, ny = member["wander_x"] + dx, member["wander_y"] + dy
    if nx < 0 or ny < 0 or nx >= cfg["map_width"] or ny >= cfg["map_height"]:
        raise FactionError("You cannot move beyond the edge of the map.")

    tile = await db.fetchone(
        "SELECT * FROM tiles WHERE guild_id=? AND x=? AND y=?", (guild_id, nx, ny)
    )
    if not tile:
        raise FactionError("That tile does not exist.")
    if tile["biome"] == "ocean":
        raise FactionError("Wanderers cannot move into open ocean.")

    await db.execute(
        "UPDATE members SET wander_x=?, wander_y=? WHERE guild_id=? AND user_id=?",
        (nx, ny, guild_id, user_id)
    )
    return tile


async def total_inhabitants(faction_row) -> int:
    decisioners = await db.fetchone(
        "SELECT COUNT(*) c FROM members WHERE faction_id=?", (faction_row["faction_id"],)
    )
    return faction_row["worker_count"] + faction_row["militia_count"] + decisioners["c"]


# --------------------------------------------------------------------------
# Creation / leaving / joining
# --------------------------------------------------------------------------

async def create_faction(guild_id: int, user_id: int, name: str, color_hex: str):
    member = await get_member(guild_id, user_id)
    if member["faction_id"]:
        raise FactionError("You must leave your current faction before creating a new one (`/faction leave`).")

    name = name.strip()
    if not (2 <= len(name) <= 32):
        raise FactionError("Faction names must be between 2 and 32 characters.")

    existing = await get_faction_by_name(guild_id, name)
    if existing:
        raise FactionError("A faction with that name already exists in this server.")

    normalized_color = await validate_color(guild_id, color_hex)

    cur = await db.execute(
        "INSERT INTO factions (guild_id, name, color_hex, leader_id, treasury, food_stock, worker_count, "
        "militia_count, created_at, is_active) VALUES (?,?,?,?,?,?,?,0,?,1)",
        (guild_id, name, normalized_color, user_id, config.STARTING_TREASURY,
         config.STARTING_FOOD_STOCK, config.STARTING_WORKERS, _now()),
    )
    faction_id = cur.lastrowid

    await db.execute(
        "UPDATE members SET faction_id=?, role='leader', wander_x=NULL, wander_y=NULL WHERE guild_id=? AND user_id=?",
        (faction_id, guild_id, user_id),
    )

    claimed_tiles = await map_manager.assign_starter_land(guild_id, faction_id)
    if not claimed_tiles:
        # roll back - no land available means the map is fully claimed
        await db.execute("UPDATE factions SET is_active=0 WHERE faction_id=?", (faction_id,))
        await db.execute(
            "UPDATE members SET faction_id=NULL, role='wanderer' WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        raise FactionError("There is no unclaimed land left on the map for a new faction to start on!")

    await log_event(guild_id, faction_id, f"**{name}** was founded by <@{user_id}>.")
    return await get_faction_by_id(faction_id)


async def leave_faction(guild_id: int, user_id: int):
    member = await get_member(guild_id, user_id)
    if not member["faction_id"]:
        raise FactionError("You're not currently in a faction.")
    faction = await get_faction_by_id(member["faction_id"])

    if faction and faction["leader_id"] == user_id:
        other_members = await db.fetchall(
            "SELECT * FROM members WHERE faction_id=? AND user_id != ?", (faction["faction_id"], user_id)
        )
        if other_members:
            raise FactionError(
                "You're the leader! Transfer leadership first with `/faction transfer` before leaving, "
                "or disband the faction with `/faction disband`."
            )
        else:
            # sole member leader leaving -> faction dissolves, land goes back to unclaimed
            await db.execute("UPDATE tiles SET owner_faction_id=NULL WHERE owner_faction_id=?", (faction["faction_id"],))
            await db.execute("UPDATE factions SET is_active=0 WHERE faction_id=?", (faction["faction_id"],))
            await log_event(guild_id, faction["faction_id"], f"**{faction['name']}** disbanded (leader left).")

    cfg = await db.fetchone("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,))
    wx = random.randint(0, cfg["map_width"] - 1)
    wy = random.randint(0, cfg["map_height"] - 1)
    await db.execute(
        "UPDATE members SET faction_id=NULL, role='wanderer', wander_x=?, wander_y=? WHERE guild_id=? AND user_id=?",
        (wx, wy, guild_id, user_id),
    )
    return True


async def invite_member(guild_id: int, inviter_id: int, target_id: int):
    """Adds `target_id` as a decisioner of the inviter's faction. Caller should already have consent
    (e.g. via a Discord button confirmation) before calling this."""
    faction = await get_member_faction(guild_id, inviter_id)
    if not faction:
        raise FactionError("You must be in a faction to invite members.")
    if faction["leader_id"] != inviter_id:
        role_row = await get_member(guild_id, inviter_id)
        if role_row["role"] not in ("leader", "officer"):
            raise FactionError("Only the faction leader or an officer can invite members.")

    target_member = await get_member(guild_id, target_id)
    if target_member["faction_id"]:
        raise FactionError("That player is already in a faction.")

    await db.execute(
        "UPDATE members SET faction_id=?, role='member', wander_x=NULL, wander_y=NULL, joined_at=? "
        "WHERE guild_id=? AND user_id=?",
        (faction["faction_id"], _now(), guild_id, target_id),
    )
    await log_event(guild_id, faction["faction_id"], f"<@{target_id}> joined **{faction['name']}** as a decisioner.")
    return faction


async def disband_faction(guild_id: int, user_id: int):
    faction = await get_member_faction(guild_id, user_id)
    if not faction:
        raise FactionError("You're not in a faction.")
    if faction["leader_id"] != user_id:
        raise FactionError("Only the leader can disband the faction.")

    await db.execute("UPDATE tiles SET owner_faction_id=NULL WHERE owner_faction_id=?", (faction["faction_id"],))
    cfg = await db.fetchone("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,))
    members = await list_faction_members(faction["faction_id"])
    for m in members:
        wx = random.randint(0, cfg["map_width"] - 1)
        wy = random.randint(0, cfg["map_height"] - 1)
        await db.execute(
            "UPDATE members SET faction_id=NULL, role='wanderer', wander_x=?, wander_y=? WHERE guild_id=? AND user_id=?",
            (wx, wy, guild_id, m["user_id"]),
        )
    await db.execute("UPDATE factions SET is_active=0 WHERE faction_id=?", (faction["faction_id"],))
    await log_event(guild_id, faction["faction_id"], f"**{faction['name']}** was disbanded by its leader.")
    return True


# --------------------------------------------------------------------------
# Leadership transfer / faction merging
# --------------------------------------------------------------------------

async def transfer_leadership(guild_id: int, current_leader_id: int, new_leader_id: int):
    """
    Two behaviors depending on who `new_leader_id` is:
      - an inhabitant (decisioner) of the SAME faction -> simple leadership handoff.
      - the leader of ANOTHER active faction -> that faction's territory, workers,
        militia and treasury are folded into this one big faction, and the other
        faction is dissolved. This is the "give control to another faction leader" case.
    """
    faction = await get_member_faction(guild_id, current_leader_id)
    if not faction:
        raise FactionError("You're not in a faction.")
    if faction["leader_id"] != current_leader_id:
        raise FactionError("Only the current leader can transfer leadership.")

    # Case 1: transferring to a member of your own faction
    target_member = await db.fetchone(
        "SELECT * FROM members WHERE guild_id=? AND user_id=? AND faction_id=?",
        (guild_id, new_leader_id, faction["faction_id"]),
    )
    if target_member:
        await db.execute("UPDATE factions SET leader_id=? WHERE faction_id=?", (new_leader_id, faction["faction_id"]))
        await db.execute(
            "UPDATE members SET role='leader' WHERE guild_id=? AND user_id=?", (guild_id, new_leader_id)
        )
        await db.execute(
            "UPDATE members SET role='member' WHERE guild_id=? AND user_id=? AND faction_id=?",
            (guild_id, current_leader_id, faction["faction_id"]),
        )
        await log_event(
            guild_id, faction["faction_id"], f"Leadership of **{faction['name']}** passed to <@{new_leader_id}>."
        )
        return {"type": "handoff", "faction": faction}

    # Case 2: new_leader_id leads ANOTHER faction -> merge (incorporate) into that faction
    other_faction = await db.fetchone(
        "SELECT * FROM factions WHERE guild_id=? AND leader_id=? AND is_active=1 AND faction_id != ?",
        (guild_id, new_leader_id, faction["faction_id"]),
    )
    if other_faction:
        await map_manager.transfer_all_land(guild_id, faction["faction_id"], other_faction["faction_id"])
        await db.execute(
            "UPDATE factions SET worker_count = worker_count + ?, militia_count = militia_count + ?, "
            "treasury = treasury + ?, food_stock = food_stock + ? WHERE faction_id=?",
            (faction["worker_count"], faction["militia_count"], faction["treasury"], faction["food_stock"],
             other_faction["faction_id"]),
        )
        await db.execute(
            "UPDATE members SET faction_id=?, role='member' WHERE faction_id=?",
            (other_faction["faction_id"], faction["faction_id"]),
        )
        await db.execute(
            "UPDATE members SET role='leader' WHERE guild_id=? AND user_id=?", (guild_id, new_leader_id)
        )
        await db.execute("UPDATE factions SET is_active=0 WHERE faction_id=?", (faction["faction_id"],))
        await log_event(
            guild_id, other_faction["faction_id"],
            f"**{faction['name']}** was incorporated into **{other_faction['name']}** by <@{current_leader_id}>."
        )
        return {"type": "merge", "into_faction": other_faction, "old_faction": faction}

    raise FactionError(
        "That user must either be a member of your faction, or the leader of another active faction, to receive control."
    )


# --------------------------------------------------------------------------
# Inhabitant requests / population transfers
# --------------------------------------------------------------------------

async def create_inhabitant_request(
    guild_id: int, requester_user_id: int, donor_faction_name: str,
    inhabitants: int, food_offer: float = 0.0, treasury_offer: float = 0.0
):
    requester = await get_member_faction(guild_id, requester_user_id)
    if not requester:
        raise FactionError("You must be in a faction to request inhabitants.")
    requester_member = await get_member(guild_id, requester_user_id)
    if requester_member["role"] not in ("leader", "officer"):
        raise FactionError("Only the faction leader or an officer can negotiate population transfers.")

    donor = await get_faction_by_name(guild_id, donor_faction_name)
    if not donor:
        raise FactionError("No such target faction.")
    if donor["faction_id"] == requester["faction_id"]:
        raise FactionError("You cannot request inhabitants from your own faction.")
    if inhabitants <= 0:
        raise FactionError("Request at least 1 inhabitant.")
    if food_offer < 0 or treasury_offer < 0:
        raise FactionError("Resource offers cannot be negative.")
    if inhabitants > donor["worker_count"]:
        raise FactionError(
            f"{donor['name']} only has {donor['worker_count']} transferable workers right now."
        )
    if food_offer > requester["food_stock"]:
        raise FactionError("You do not have enough food for that offer.")
    if treasury_offer > requester["treasury"]:
        raise FactionError("You do not have enough money for that offer.")

    cur = await db.execute(
        "INSERT INTO inhabitant_requests "
        "(guild_id, requester_faction_id, donor_faction_id, inhabitants, food_offer, treasury_offer, status, created_at) "
        "VALUES (?,?,?,?,?,?, 'pending', ?)",
        (guild_id, requester["faction_id"], donor["faction_id"], inhabitants,
         food_offer, treasury_offer, _now()),
    )
    return await db.fetchone("SELECT * FROM inhabitant_requests WHERE request_id=?", (cur.lastrowid,))


async def resolve_inhabitant_request(guild_id: int, donor_user_id: int, request_id: int, accept: bool):
    request = await db.fetchone(
        "SELECT * FROM inhabitant_requests WHERE guild_id=? AND request_id=? AND status='pending'",
        (guild_id, request_id)
    )
    if not request:
        raise FactionError("That inhabitant request is no longer pending.")

    donor = await get_member_faction(guild_id, donor_user_id)
    if not donor or donor["faction_id"] != request["donor_faction_id"]:
        raise FactionError("Only members of the requested faction can answer this request.")
    donor_member = await get_member(guild_id, donor_user_id)
    if donor_member["role"] not in ("leader", "officer"):
        raise FactionError("Only the donor faction's leader or an officer can answer this request.")

    requester = await get_faction_by_id(request["requester_faction_id"])
    if not requester:
        raise FactionError("The requesting faction no longer exists.")

    if not accept:
        await db.execute(
            "UPDATE inhabitant_requests SET status='declined' WHERE request_id=? AND status='pending'",
            (request_id,)
        )
        return {"status": "declined", "request": request, "requester": requester, "donor": donor}

    if donor["worker_count"] < request["inhabitants"]:
        raise FactionError(f"{donor['name']} no longer has enough transferable workers.")
    if requester["food_stock"] < request["food_offer"]:
        raise FactionError("The requesting faction no longer has enough food for the promised offer.")
    if requester["treasury"] < request["treasury_offer"]:
        raise FactionError("The requesting faction no longer has enough money for the promised offer.")

    async with db.transaction() as conn:
        donor_update = await conn.execute(
            "UPDATE factions SET worker_count=worker_count-? WHERE faction_id=? AND worker_count>=?",
            (request["inhabitants"], donor["faction_id"], request["inhabitants"])
        )
        if donor_update.rowcount != 1:
            raise FactionError("The donor faction no longer has enough workers.")

        requester_update = await conn.execute(
            "UPDATE factions SET food_stock=food_stock-?, treasury=treasury-?, worker_count=worker_count+? "
            "WHERE faction_id=? AND food_stock>=? AND treasury>=?",
            (request["food_offer"], request["treasury_offer"], request["inhabitants"],
             requester["faction_id"], request["food_offer"], request["treasury_offer"])
        )
        if requester_update.rowcount != 1:
            raise FactionError("The requesting faction no longer has the promised resources.")

        await conn.execute(
            "UPDATE factions SET food_stock=food_stock+?, treasury=treasury+? WHERE faction_id=?",
            (request["food_offer"], request["treasury_offer"], donor["faction_id"])
        )
        status_update = await conn.execute(
            "UPDATE inhabitant_requests SET status='accepted' WHERE request_id=? AND status='pending'",
            (request_id,)
        )
        if status_update.rowcount != 1:
            raise FactionError("That request was already resolved.")

    await log_event(
        guild_id,
        requester["faction_id"],
        f"Received {request['inhabitants']} workers from {donor['name']} for {request['food_offer']:.1f} food and {request['treasury_offer']:.1f} money.",
    )
    await log_event(
        guild_id,
        donor["faction_id"],
        f"Transferred {request['inhabitants']} workers to {requester['name']} for {request['food_offer']:.1f} food and {request['treasury_offer']:.1f} money.",
    )
    return {"status": "accepted", "request": request, "requester": requester, "donor": donor}


# --------------------------------------------------------------------------
# Economy helpers used by the simulation & disasters
# --------------------------------------------------------------------------

async def adjust_worker_count(faction_id: int, delta: int):
    await db.execute(
        "UPDATE factions SET worker_count = MAX(0, worker_count + ?) WHERE faction_id=?", (delta, faction_id)
    )


async def adjust_treasury(faction_id: int, delta: float):
    await db.execute("UPDATE factions SET treasury = treasury + ? WHERE faction_id=?", (delta, faction_id))


async def adjust_food(faction_id: int, delta: float):
    await db.execute("UPDATE factions SET food_stock = food_stock + ? WHERE faction_id=?", (delta, faction_id))


async def adjust_militia(faction_id: int, delta: int):
    await db.execute(
        "UPDATE factions SET militia_count = MAX(0, militia_count + ?) WHERE faction_id=?", (delta, faction_id)
    )


async def list_active_factions(guild_id: int):
    return await db.fetchall("SELECT * FROM factions WHERE guild_id=? AND is_active=1", (guild_id,))


async def log_event(guild_id: int, faction_id: int | None, message: str, day: int | None = None):
    await db.execute(
        "INSERT INTO events_log (guild_id, faction_id, day, message, created_at) VALUES (?,?,?,?,?)",
        (guild_id, faction_id, day, message, _now()),
    )


async def recent_events(guild_id: int, faction_id: int | None = None, limit: int = 10):
    if faction_id:
        return await db.fetchall(
            "SELECT * FROM events_log WHERE guild_id=? AND faction_id=? ORDER BY id DESC LIMIT ?",
            (guild_id, faction_id, limit),
        )
    return await db.fetchall(
        "SELECT * FROM events_log WHERE guild_id=? ORDER BY id DESC LIMIT ?", (guild_id, limit)
    )


async def claim_wanderers_in_conquered_tiles(guild_id: int, faction_id: int, tiles: list[tuple[int, int]]):
    """When a faction conquers land, any wanderer physically standing on those tiles
    automatically becomes a decisioner of the conquering faction."""
    if not tiles:
        return []
    joined = []
    for (x, y) in tiles:
        wanderers = await db.fetchall(
            "SELECT * FROM members WHERE guild_id=? AND faction_id IS NULL AND wander_x=? AND wander_y=?",
            (guild_id, x, y),
        )
        for w in wanderers:
            await db.execute(
                "UPDATE members SET faction_id=?, role='member', wander_x=NULL, wander_y=NULL WHERE guild_id=? AND user_id=?",
                (faction_id, guild_id, w["user_id"]),
            )
            joined.append(w["user_id"])
    if joined:
        faction = await get_faction_by_id(faction_id)
        names = ", ".join(f"<@{u}>" for u in joined)
        await log_event(guild_id, faction_id, f"{names} were absorbed into **{faction['name']}** as their land was conquered.")
    return joined
