"""
Central configuration for the Faction Empires bot.
Tweak these values to rebalance the simulation without touching game logic.
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # reads a .env file in the current directory, if present, into os.environ
except ImportError:
    pass  # python-dotenv not installed - fall back to whatever is already in the environment

# --- Discord ---
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

# --- Database ---
DB_PATH = os.environ.get("FACTION_BOT_DB", os.path.join(os.path.dirname(__file__), "data", "world.db"))

# --- Map ---
MAP_WIDTH = 48
MAP_HEIGHT = 48
STARTER_LAND_TILES = 6          # how many tiles a brand new faction starts with
MAX_MAP_RENDER_PX = 640         # rendered PNG size (square)

# Biome elevation/moisture thresholds (elevation in roughly -1..1)
SEA_LEVEL = -0.05
BEACH_LEVEL = 0.0
HILL_LEVEL = 0.28
MOUNTAIN_LEVEL = 0.5
PEAK_LEVEL = 0.72

# --- Simulation tick ---
DEFAULT_TICK_HOURS = 24.0       # how often the world advances one "day"
DISASTER_CHANCE = 0.10          # 10% chance per guild per tick

# --- Economy ---
STARTING_TREASURY = 250.0
STARTING_FOOD_STOCK = 250.0
STARTING_WORKERS = 40

FOOD_PER_WORKER_BASE = 1.0       # base food produced per worker per tick
MONEY_PER_WORKER_BASE = 0.8      # base money produced per worker per tick
FOOD_CONSUMED_PER_CAPITA = 0.9   # food eaten per inhabitant (workers+decisioners+militia) per tick
MILITIA_UPKEEP_MONEY = 0.5       # militia cost more to maintain than workers

# Biome production modifiers: (food_mult, money_mult)
BIOME_MODIFIERS = {
    "ocean":     (0.0, 0.0),
    "beach":     (0.8, 0.6),
    "plains":    (1.5, 0.8),
    "forest":    (1.1, 1.0),
    "desert":    (0.4, 0.7),
    "hills":     (0.8, 1.3),
    "mountain":  (0.3, 1.8),
    "peak":      (0.1, 2.2),
}

# Starvation: if food_stock goes negative, this fraction of the shortage (in "food units")
# translates into deaths, applied against total inhabitants.
STARVATION_DEATH_RATE = 0.02

# Migration: if a faction's money-per-capita falls below this, some workers emigrate
# to the richest faction in the same guild with better money-per-capita.
POVERTY_MONEY_PER_CAPITA_THRESHOLD = 0.15
MIGRATION_FRACTION = 0.05         # fraction of workers that leave per tick when poor

# --- Colors ---
# Minimum "redmean" perceptual color distance required between two faction colors
# in the same guild. Lower = colors can be more similar.
MIN_COLOR_DISTANCE = 90.0

# --- Military ---
MILITIA_FOOD_UPKEEP = 0.3
CONQUER_BASE_SUCCESS = 0.55       # base chance to conquer an adjacent unclaimed/enemy tile
TERRAIN_DEFENSE_BONUS = {          # defender bonus by biome of the contested tile
    "plains": 0.0,
    "beach": 0.0,
    "forest": 0.10,
    "hills": 0.20,
    "mountain": 0.35,
    "peak": 0.45,
    "desert": 0.05,
    "ocean": 1.0,  # effectively unconquerable
}
SCOUT_MILITIA_COST = 5
SCOUT_LOSS_CHANCE = 0.05
ATTACK_COOLDOWN_HOURS = 12
CONQUER_COOLDOWN_HOURS = 6

# --- Misc ---
EMBED_COLOR_DEFAULT = 0x2ECC71
