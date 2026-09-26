"""
Owns everything related to the persistent per-guild world map:
 - one-time terrain generation (persisted forever, seeded)
 - daily snapshotting ("memory of the previous map")
 - faction territory / ownership queries
 - starter-land assignment for new factions
 - PNG rendering (terrain + faction color overlay)
 - random catastrophic disasters
"""
import gzip
import io
import json
import math
import random
from datetime import datetime, timezone

from PIL import Image, ImageDraw

import config
import db
from world.noise_gen import TerrainGenerator, BIOME_COLORS

DISASTER_KINDS = [
    ("earthquake", "An earthquake"),
    ("flood", "A catastrophic flood"),
    ("wildfire", "A raging wildfire"),
    ("plague", "A plague outbreak"),
    ("storm", "A megastorm"),
]

NEIGHBOR_OFFSETS = [(1, 0), (-1, 0), (0, 1), (0, -1)]


def _now():
    return datetime.now(timezone.utc).isoformat()


async def guild_has_map(guild_id: int) -> bool:
    row = await db.fetchone("SELECT 1 FROM guild_config WHERE guild_id=?", (guild_id,))
    return row is not None


async def generate_world(guild_id: int, seed: int | None = None,
                          width: int = config.MAP_WIDTH, height: int = config.MAP_HEIGHT):
    """Generates brand-new terrain for a guild. Destroys any existing map for that guild."""
    if seed is None:
        seed = random.randint(0, 2_000_000_000)

    gen = TerrainGenerator(seed)
    grid = gen.generate(width, height)

    await db.execute("DELETE FROM tiles WHERE guild_id=?", (guild_id,))
    await db.execute(
        """INSERT INTO guild_config (guild_id, map_seed, map_width, map_height, day_count, last_tick_at, tick_hours)
           VALUES (?,?,?,?,0,?,?)
           ON CONFLICT(guild_id) DO UPDATE SET map_seed=excluded.map_seed, map_width=excluded.map_width,
               map_height=excluded.map_height, day_count=0, last_tick_at=excluded.last_tick_at""",
        (guild_id, seed, width, height, _now(), config.DEFAULT_TICK_HOURS),
    )

    rows = []
    for row in grid:
        for tile in row:
            rows.append((guild_id, tile.x, tile.y, tile.elevation, tile.moisture, tile.biome, None))
    await db.executemany(
        "INSERT INTO tiles (guild_id, x, y, elevation, moisture, biome, owner_faction_id) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    return seed, width, height


async def get_tiles(guild_id: int):
    return await db.fetchall("SELECT * FROM tiles WHERE guild_id=?", (guild_id,))


async def get_land_tiles(guild_id: int):
    """Non-ocean tiles - the only ones factions can ever own."""
    return await db.fetchall("SELECT * FROM tiles WHERE guild_id=? AND biome != 'ocean'", (guild_id,))


async def get_unclaimed_land(guild_id: int):
    return await db.fetchall(
        "SELECT * FROM tiles WHERE guild_id=? AND biome != 'ocean' AND owner_faction_id IS NULL", (guild_id,)
    )


async def get_faction_tiles(guild_id: int, faction_id: int):
    return await db.fetchall(
        "SELECT * FROM tiles WHERE guild_id=? AND owner_faction_id=?", (guild_id, faction_id)
    )


async def count_faction_land(guild_id: int, faction_id: int) -> int:
    row = await db.fetchone(
        "SELECT COUNT(*) c FROM tiles WHERE guild_id=? AND owner_faction_id=?", (guild_id, faction_id)
    )
    return row["c"] if row else 0


async def assign_starter_land(guild_id: int, faction_id: int, n_tiles: int = config.STARTER_LAND_TILES):
    """
    Picks a random unclaimed land tile as a seed point, then grows a small
    contiguous blob around it via BFS so new factions start with a
    connected territory rather than scattered tiles.
    """
    unclaimed = await get_unclaimed_land(guild_id)
    if not unclaimed:
        return []

    unclaimed_set = {(t["x"], t["y"]) for t in unclaimed}
    start = random.choice(list(unclaimed_set))

    claimed = []
    frontier = [start]
    visited = {start}
    while frontier and len(claimed) < n_tiles:
        cx, cy = frontier.pop(0)
        if (cx, cy) in unclaimed_set:
            claimed.append((cx, cy))
        neighbors = [(cx + dx, cy + dy) for dx, dy in NEIGHBOR_OFFSETS]
        random.shuffle(neighbors)
        for nx, ny in neighbors:
            if (nx, ny) in unclaimed_set and (nx, ny) not in visited:
                visited.add((nx, ny))
                frontier.append((nx, ny))

    if claimed:
        await db.executemany(
            "UPDATE tiles SET owner_faction_id=? WHERE guild_id=? AND x=? AND y=?",
            [(faction_id, guild_id, x, y) for x, y in claimed],
        )
    return claimed


async def transfer_all_land(guild_id: int, from_faction_id: int, to_faction_id: int):
    """Used when one faction is incorporated into another."""
    await db.execute(
        "UPDATE tiles SET owner_faction_id=? WHERE guild_id=? AND owner_faction_id=?",
        (to_faction_id, guild_id, from_faction_id),
    )


async def release_random_land(guild_id: int, faction_id: int, n_tiles: int):
    """Used when a faction loses a battle or suffers a disaster wipeout - drops n random border/owned tiles."""
    tiles = await get_faction_tiles(guild_id, faction_id)
    if not tiles:
        return
    n_tiles = min(n_tiles, len(tiles))
    to_release = random.sample(tiles, n_tiles)
    await db.executemany(
        "UPDATE tiles SET owner_faction_id=NULL WHERE guild_id=? AND x=? AND y=?",
        [(guild_id, t["x"], t["y"]) for t in to_release],
    )


async def get_adjacent_unclaimed_or_enemy(guild_id: int, faction_id: int):
    """Returns land tiles adjacent to the faction's territory that are either unclaimed or owned by someone else."""
    owned = await get_faction_tiles(guild_id, faction_id)
    owned_set = {(t["x"], t["y"]) for t in owned}
    if not owned_set:
        return []
    all_tiles = {(t["x"], t["y"]): t for t in await get_tiles(guild_id)}
    candidates = {}
    for (x, y) in owned_set:
        for dx, dy in NEIGHBOR_OFFSETS:
            key = (x + dx, y + dy)
            if key in owned_set or key in candidates:
                continue
            tile = all_tiles.get(key)
            if tile and tile["biome"] != "ocean" and tile["owner_faction_id"] != faction_id:
                candidates[key] = tile
    return list(candidates.values())


# --------------------------------------------------------------------------
# Snapshots ("memory" of the map across days)
# --------------------------------------------------------------------------

async def snapshot_map(guild_id: int, day: int):
    tiles = await get_tiles(guild_id)
    payload = [
        {"x": t["x"], "y": t["y"], "biome": t["biome"], "owner": t["owner_faction_id"]}
        for t in tiles
    ]
    blob = gzip.compress(json.dumps(payload).encode("utf-8"))
    await db.execute(
        "INSERT INTO map_snapshots (guild_id, day, data, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(guild_id, day) DO UPDATE SET data=excluded.data, created_at=excluded.created_at",
        (guild_id, day, blob, _now()),
    )


async def load_snapshot(guild_id: int, day: int):
    row = await db.fetchone("SELECT data FROM map_snapshots WHERE guild_id=? AND day=?", (guild_id, day))
    if not row:
        return None
    return json.loads(gzip.decompress(row["data"]).decode("utf-8"))


async def list_snapshot_days(guild_id: int):
    rows = await db.fetchall(
        "SELECT day FROM map_snapshots WHERE guild_id=? ORDER BY day DESC", (guild_id,)
    )
    return [r["day"] for r in rows]


# --------------------------------------------------------------------------
# Disasters
# --------------------------------------------------------------------------

async def maybe_trigger_disaster(guild_id: int, day: int, radius: int = 3):
    """10% chance (config.DISASTER_CHANCE). Returns a dict describing the disaster, or None."""
    if random.random() > config.DISASTER_CHANCE:
        return None

    land = await get_land_tiles(guild_id)
    if not land:
        return None
    center = random.choice(land)
    kind_key, kind_label = random.choice(DISASTER_KINDS)

    affected = await db.fetchall(
        "SELECT * FROM tiles WHERE guild_id=? AND biome != 'ocean' "
        "AND (x-?)*(x-?) + (y-?)*(y-?) <= ?",
        (guild_id, center["x"], center["x"], center["y"], center["y"], radius * radius),
    )

    casualties_total = 0
    affected_factions = {}
    for tile in affected:
        fid = tile["owner_faction_id"]
        if fid:
            affected_factions.setdefault(fid, 0)
            affected_factions[fid] += 1

    from factions import manager as faction_manager  # local import avoids circular import
    for fid, tiles_hit in affected_factions.items():
        faction = await faction_manager.get_faction_by_id(fid)
        if not faction:
            continue
        inhabitants = await faction_manager.total_inhabitants(faction)
        # more tiles hit -> proportionally more of the population caught in the disaster
        exposure = min(1.0, tiles_hit / max(1, await count_faction_land(guild_id, fid)))
        deaths = int(inhabitants * exposure * random.uniform(0.05, 0.20))
        deaths = min(deaths, max(0, faction["worker_count"]))
        if deaths > 0:
            await faction_manager.adjust_worker_count(fid, -deaths)
            casualties_total += deaths

    await db.execute(
        "INSERT INTO disasters_log (guild_id, day, x, y, radius, kind, casualties, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (guild_id, day, center["x"], center["y"], radius, kind_key, casualties_total, _now()),
    )

    return {
        "kind": kind_key,
        "label": kind_label,
        "x": center["x"],
        "y": center["y"],
        "radius": radius,
        "casualties": casualties_total,
        "factions_hit": list(affected_factions.keys()),
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _hex_to_rgb(hex_color: str):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


async def render_map_png(guild_id: int) -> bytes:
    cfg = await db.fetchone("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,))
    if not cfg:
        raise ValueError("This server has no world yet.")
    width, height = cfg["map_width"], cfg["map_height"]

    tiles = await get_tiles(guild_id)
    factions = await db.fetchall("SELECT faction_id, color_hex FROM factions WHERE guild_id=? AND is_active=1", (guild_id,))
    color_map = {f["faction_id"]: _hex_to_rgb(f["color_hex"]) for f in factions}

    img = Image.new("RGB", (width, height))
    for t in tiles:
        base = BIOME_COLORS[t["biome"]]
        if t["owner_faction_id"] and t["owner_faction_id"] in color_map:
            owner_color = color_map[t["owner_faction_id"]]
            # Mostly faction color with a thin trace of terrain shade so biome texture stays legible.
            blended = tuple(int(base[i] * 0.18 + owner_color[i] * 0.82) for i in range(3))
            img.putpixel((t["x"], t["y"]), blended)
        else:
            img.putpixel((t["x"], t["y"]), base)

    scale = max(1, config.MAX_MAP_RENDER_PX // max(width, height))
    img = img.resize((width * scale, height * scale), Image.NEAREST)

    # Draw thin dark outlines wherever faction ownership changes between neighboring
    # tiles, so territory borders are legible even when two factions share a hue.
    owner_by_xy = {(t["x"], t["y"]): t["owner_faction_id"] for t in tiles}
    line_w = max(1, scale // 6)
    border_color = (25, 25, 25)
    draw = ImageDraw.Draw(img)
    for (x, y), owner in owner_by_xy.items():
        right = owner_by_xy.get((x + 1, y), "OUT")
        down = owner_by_xy.get((x, y + 1), "OUT")
        if right != owner and (owner is not None or right not in (None, "OUT")):
            px = (x + 1) * scale
            draw.line([(px, y * scale), (px, (y + 1) * scale)], fill=border_color, width=line_w)
        if down != owner and (owner is not None or down not in (None, "OUT")):
            py = (y + 1) * scale
            draw.line([(x * scale, py), ((x + 1) * scale, py)], fill=border_color, width=line_w)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
