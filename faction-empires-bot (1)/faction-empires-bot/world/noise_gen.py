"""
Realistic-looking procedural terrain generator.

Technique (as requested):
  1. Fractal Simplex noise (fBm: fractal Brownian motion - stacked octaves of
     OpenSimplex noise at increasing frequency / decreasing amplitude).
  2. Domain warping - the coordinates fed into the elevation noise are
     themselves perturbed by two extra noise fields, which breaks up the
     "blobby" look of raw fBm and produces winding coastlines / ridgelines.
  3. The "gradient trick" - a radial falloff subtracted from elevation so
     that the map naturally forms island-like continents with ocean at the
     edges, instead of an infinite tiling texture.

Everything is deterministic given a seed, so a guild's map can be
regenerated identically at any time (e.g. to redraw history).
"""
import math
from dataclasses import dataclass
from opensimplex import OpenSimplex

import config


@dataclass
class TileData:
    x: int
    y: int
    elevation: float
    moisture: float
    biome: str


class TerrainGenerator:
    def __init__(self, seed: int):
        self.seed = seed
        # Independent noise sources derived from the same seed so the whole
        # map is reproducible from a single integer.
        self._elev_src = OpenSimplex(seed=seed)
        self._warp_x_src = OpenSimplex(seed=seed + 10_007)
        self._warp_y_src = OpenSimplex(seed=seed + 20_011)
        self._moist_src = OpenSimplex(seed=seed + 30_017)

    @staticmethod
    def _fbm(src: OpenSimplex, x: float, y: float, octaves: int, persistence: float,
             lacunarity: float, scale: float) -> float:
        amplitude = 1.0
        frequency = 1.0
        total = 0.0
        max_amp = 0.0
        for _ in range(octaves):
            total += src.noise2(x * frequency * scale, y * frequency * scale) * amplitude
            max_amp += amplitude
            amplitude *= persistence
            frequency *= lacunarity
        return total / max_amp if max_amp else 0.0

    def _warp(self, x: float, y: float, strength: float = 0.65):
        wx = self._fbm(self._warp_x_src, x, y, octaves=4, persistence=0.5, lacunarity=2.0, scale=1.0)
        wy = self._fbm(self._warp_y_src, x, y, octaves=4, persistence=0.5, lacunarity=2.0, scale=1.0)
        return x + wx * strength, y + wy * strength

    def elevation_at(self, nx: float, ny: float) -> float:
        """nx, ny expected roughly in [-1, 1] (normalized map coordinates)."""
        wx, wy = self._warp(nx, ny)
        e = self._fbm(self._elev_src, wx, wy, octaves=6, persistence=0.5, lacunarity=2.05, scale=1.3)

        # Gradient trick: push elevation down near the edges of the map so
        # continents form near the center and oceans ring the border.
        d = math.sqrt(nx * nx + ny * ny) / math.sqrt(2.0)  # 0 at center, 1 at corner
        gradient = d ** 3
        e = e - gradient * 1.1
        return e

    def moisture_at(self, nx: float, ny: float) -> float:
        wx, wy = self._warp(nx, ny, strength=0.4)
        return self._fbm(self._moist_src, wx, wy, octaves=4, persistence=0.5, lacunarity=2.0, scale=2.2)

    @staticmethod
    def classify_biome(elevation: float, moisture: float) -> str:
        if elevation < config.SEA_LEVEL:
            return "ocean"
        if elevation < config.BEACH_LEVEL:
            return "beach"
        if elevation < config.HILL_LEVEL:
            # Lowlands: split by moisture into plains / forest / desert
            if moisture < -0.15:
                return "desert"
            if moisture > 0.2:
                return "forest"
            return "plains"
        if elevation < config.MOUNTAIN_LEVEL:
            return "hills"
        if elevation < config.PEAK_LEVEL:
            return "mountain"
        return "peak"

    def generate(self, width: int, height: int) -> list[list[TileData]]:
        """Returns a 2D grid [y][x] of TileData."""
        grid: list[list[TileData]] = []
        for y in range(height):
            row = []
            ny = (y / (height - 1)) * 2 - 1
            for x in range(width):
                nx = (x / (width - 1)) * 2 - 1
                elevation = self.elevation_at(nx, ny)
                moisture = self.moisture_at(nx, ny)
                biome = self.classify_biome(elevation, moisture)
                row.append(TileData(x=x, y=y, elevation=elevation, moisture=moisture, biome=biome))
            grid.append(row)
        return grid


BIOME_COLORS = {
    "ocean":    (32, 80, 152),
    "beach":    (231, 214, 156),
    "plains":   (140, 196, 90),
    "forest":   (55, 122, 61),
    "desert":   (222, 191, 116),
    "hills":    (150, 140, 90),
    "mountain": (120, 110, 110),
    "peak":     (245, 245, 250),
}
