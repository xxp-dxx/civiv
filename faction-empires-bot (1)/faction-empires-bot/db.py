"""
Thin async SQLite access layer shared by every module.
Uses aiosqlite so the whole bot stays non-blocking.
"""
import aiosqlite
import os
from contextlib import asynccontextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id        INTEGER PRIMARY KEY,
    map_seed        INTEGER NOT NULL,
    map_width       INTEGER NOT NULL,
    map_height      INTEGER NOT NULL,
    day_count       INTEGER NOT NULL DEFAULT 0,
    last_tick_at    TEXT,
    announce_channel_id INTEGER,
    tick_hours      REAL NOT NULL DEFAULT 0.3333333333
);

CREATE TABLE IF NOT EXISTS factions (
    faction_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    name         TEXT NOT NULL,
    color_hex    TEXT NOT NULL,
    leader_id    INTEGER NOT NULL,
    treasury     REAL NOT NULL DEFAULT 0,
    food_stock   REAL NOT NULL DEFAULT 0,
    worker_count INTEGER NOT NULL DEFAULT 0,
    militia_count INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    is_active    INTEGER NOT NULL DEFAULT 1,
    UNIQUE(guild_id, name)
);

CREATE TABLE IF NOT EXISTS members (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    faction_id INTEGER,
    role       TEXT NOT NULL DEFAULT 'member',
    wander_x   INTEGER,
    wander_y   INTEGER,
    joined_at  TEXT,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS tiles (
    guild_id INTEGER NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    elevation REAL NOT NULL,
    moisture REAL NOT NULL,
    biome TEXT NOT NULL,
    owner_faction_id INTEGER,
    PRIMARY KEY (guild_id, x, y)
);
CREATE INDEX IF NOT EXISTS idx_tiles_owner ON tiles(guild_id, owner_faction_id);

CREATE TABLE IF NOT EXISTS map_snapshots (
    guild_id   INTEGER NOT NULL,
    day        INTEGER NOT NULL,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, day)
);

CREATE TABLE IF NOT EXISTS stats_history (
    guild_id   INTEGER NOT NULL,
    faction_id INTEGER NOT NULL,
    day        INTEGER NOT NULL,
    food_prod  REAL NOT NULL,
    money_prod REAL NOT NULL,
    food_consumed REAL NOT NULL,
    inhabitants INTEGER NOT NULL,
    inhabitants_delta INTEGER NOT NULL,
    land_count INTEGER NOT NULL,
    PRIMARY KEY (faction_id, day)
);

CREATE TABLE IF NOT EXISTS diplomacy (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    faction_a  INTEGER NOT NULL,
    faction_b  INTEGER NOT NULL,
    kind       TEXT NOT NULL,      -- 'alliance' or 'trade'
    status     TEXT NOT NULL,      -- 'pending' / 'active' / 'broken' / 'rejected'
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS disasters_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    day INTEGER NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    radius INTEGER NOT NULL,
    kind TEXT NOT NULL,
    casualties INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    faction_id INTEGER,
    day INTEGER,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cooldowns (
    guild_id INTEGER NOT NULL,
    faction_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    PRIMARY KEY (faction_id, action)
);

CREATE TABLE IF NOT EXISTS inhabitant_requests (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    requester_faction_id INTEGER NOT NULL,
    donor_faction_id INTEGER NOT NULL,
    inhabitants INTEGER NOT NULL,
    food_offer REAL NOT NULL DEFAULT 0,
    treasury_offer REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inhabitant_requests_target
    ON inhabitant_requests(guild_id, donor_faction_id, status);
"""

_connection: aiosqlite.Connection | None = None


async def init_db():
    global _connection
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    _connection = await aiosqlite.connect(config.DB_PATH)
    _connection.row_factory = aiosqlite.Row
    await _connection.executescript(SCHEMA)
    # Migrate worlds created under the old 24-hour default to the new default.
    # Explicitly customized intervals remain unchanged.
    await _connection.execute(
        "UPDATE guild_config SET tick_hours=? WHERE tick_hours=24.0",
        (config.DEFAULT_TICK_HOURS,),
    )
    await _connection.commit()
    return _connection


async def close_db():
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None


def get_conn() -> aiosqlite.Connection:
    if _connection is None:
        raise RuntimeError("Database not initialised yet - call init_db() first.")
    return _connection


async def execute(query: str, params: tuple = ()):
    conn = get_conn()
    cur = await conn.execute(query, params)
    await conn.commit()
    return cur


async def executemany(query: str, seq_of_params):
    conn = get_conn()
    cur = await conn.executemany(query, seq_of_params)
    await conn.commit()
    return cur


async def fetchone(query: str, params: tuple = ()):
    conn = get_conn()
    cur = await conn.execute(query, params)
    row = await cur.fetchone()
    await cur.close()
    return row


async def fetchall(query: str, params: tuple = ()):
    conn = get_conn()
    cur = await conn.execute(query, params)
    rows = await cur.fetchall()
    await cur.close()
    return rows


@asynccontextmanager
async def transaction():
    """Simple context manager for grouping several writes into one commit."""
    conn = get_conn()
    try:
        yield conn
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
