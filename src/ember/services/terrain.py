"""Terrain service for multi-layer LANDFIRE queries.

Queries multiple raster layers in parallel and returns combined terrain data
for fire behavior modeling.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from time import time
from typing import Any

from ember.config import settings
from ember.logging import get_logger
from ember.services.cog import COGService, get_cog_service

logger = get_logger(__name__)

# Simple in-memory cache for terrain queries
# LANDFIRE data is static, so long TTL is fine
_terrain_cache: dict[str, dict] = {}
_CACHE_TTL_SECONDS = 1800  # 30 minutes
_CACHE_MAX_SIZE = 1000  # Max entries before purge

# Layer name -> file pattern mapping
# Pattern matches: LC{YY}_{PATTERN}_{RES}.tif
LAYER_PATTERNS = {
    "fuel": "F40",           # FBFM40 fuel model (91-204 -> NB1, GR2, SH5, etc.)
    "slope": "SlpD",         # Slope in degrees (0-90)
    "aspect": "Asp",         # Aspect in degrees (0-360, -1 = flat)
    "elevation": "Elev",     # Elevation in meters
    "canopy_height": "CH",   # Canopy height in meters * 10
    "canopy_base_height": "CBH",  # Canopy base height in meters * 10
    "canopy_bulk_density": "CBD", # Canopy bulk density kg/m³ * 100
    "canopy_cover": "CC",    # Canopy cover percent (0-100)
}

# FBFM40 pixel value -> fuel code mapping
FUEL_CODES = {
    91: "NB1", 92: "NB2", 93: "NB3", 98: "NB8", 99: "NB9",
    101: "GR1", 102: "GR2", 103: "GR3", 104: "GR4", 105: "GR5",
    106: "GR6", 107: "GR7", 108: "GR8", 109: "GR9",
    121: "GS1", 122: "GS2", 123: "GS3", 124: "GS4",
    141: "SH1", 142: "SH2", 143: "SH3", 144: "SH4", 145: "SH5",
    146: "SH6", 147: "SH7", 148: "SH8", 149: "SH9",
    161: "TU1", 162: "TU2", 163: "TU3", 164: "TU4", 165: "TU5",
    181: "TL1", 182: "TL2", 183: "TL3", 184: "TL4", 185: "TL5",
    186: "TL6", 187: "TL7", 188: "TL8", 189: "TL9",
    201: "SB1", 202: "SB2", 203: "SB3", 204: "SB4",
}


class TerrainService:
    """Service for querying multiple LANDFIRE layers."""

    def __init__(self, s3_prefix: str):
        """
        Initialize terrain service.

        Args:
            s3_prefix: S3 prefix containing LANDFIRE TIF files
                       (e.g., s3://stellaris-landfire-data/Tif)
        """
        self.s3_prefix = s3_prefix.rstrip("/")
        self._layer_urls: dict[str, str] = {}
        self._cog_services: dict[str, COGService] = {}
        self._executor = ThreadPoolExecutor(max_workers=8)

    def register_layer(self, layer_name: str, filename: str) -> None:
        """
        Register a layer with its filename.

        Args:
            layer_name: Layer name (fuel, slope, etc.)
            filename: TIF filename (e.g., LC20_SlpD_220.tif)
        """
        url = f"{self.s3_prefix}/{filename}"
        self._layer_urls[layer_name] = url
        logger.info(f"Registered layer: {layer_name} -> {url}")

    def discover_layers(self, available_files: list[str]) -> dict[str, str]:
        """
        Discover available layers from a list of filenames.

        Args:
            available_files: List of TIF filenames in S3

        Returns:
            Dict of layer_name -> filename for discovered layers
        """
        discovered = {}
        for layer_name, pattern in LAYER_PATTERNS.items():
            for filename in available_files:
                if f"_{pattern}_" in filename and filename.endswith(".tif"):
                    discovered[layer_name] = filename
                    self.register_layer(layer_name, filename)
                    break
        return discovered

    def _get_cog_service(self, layer_name: str) -> COGService | None:
        """Get or create COG service for a layer."""
        if layer_name not in self._layer_urls:
            return None

        if layer_name not in self._cog_services:
            url = self._layer_urls[layer_name]
            self._cog_services[layer_name] = get_cog_service(url)

        return self._cog_services[layer_name]

    def _query_layer(self, layer_name: str, lat: float, lon: float) -> dict[str, Any]:
        """Query a single layer (runs in thread pool)."""
        cog = self._get_cog_service(layer_name)
        if not cog:
            return {"layer": layer_name, "status": "not_configured"}

        result = cog.point_query(lat, lon)
        return {"layer": layer_name, **result}

    async def query_terrain(
        self,
        lat: float,
        lon: float,
        layers: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Query multiple terrain layers at a point.

        Args:
            lat: Latitude
            lon: Longitude
            layers: Specific layers to query (default: all registered)

        Returns:
            Combined terrain data
        """
        if layers is None:
            layers = list(self._layer_urls.keys())

        # Check cache (round to 4 decimals = ~11m precision)
        cache_key = f"{lat:.4f},{lon:.4f}:{','.join(sorted(layers))}"
        cached = _terrain_cache.get(cache_key)
        if cached and (time() - cached["timestamp"] < _CACHE_TTL_SECONDS):
            logger.debug(f"Cache hit for {cache_key}")
            return cached["data"]

        # Query all layers in parallel using thread pool
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(self._executor, self._query_layer, layer, lat, lon)
            for layer in layers
        ]
        results = await asyncio.gather(*tasks)

        # Build response
        response = {
            "latitude": lat,
            "longitude": lon,
            "layers_queried": layers,
        }

        for result in results:
            layer = result.pop("layer")
            if result.get("status") == "success":
                value = result.get("value")
                response[layer] = self._transform_value(layer, value)
            else:
                response[layer] = None

        # Store in cache
        if len(_terrain_cache) >= _CACHE_MAX_SIZE:
            _terrain_cache.clear()  # Simple purge when full
        _terrain_cache[cache_key] = {"timestamp": time(), "data": response}

        return response

    def _transform_value(self, layer: str, value: Any) -> Any:
        """Transform raw pixel value to meaningful data."""
        if value is None:
            return None

        if layer == "fuel":
            code = FUEL_CODES.get(value, f"Unknown({value})")
            return {"code": code, "raw": value}

        if layer == "aspect":
            # -1 means flat
            if value == -1:
                return {"degrees": None, "direction": "flat"}
            direction = self._aspect_to_direction(value)
            return {"degrees": value, "direction": direction}

        if layer == "slope":
            return {"degrees": value}

        if layer == "elevation":
            return {"meters": value}

        if layer in ("canopy_height", "canopy_base_height"):
            # Stored as meters * 10
            return {"meters": value / 10.0 if value else None}

        if layer == "canopy_bulk_density":
            # Stored as kg/m³ * 100
            return {"kg_per_m3": value / 100.0 if value else None}

        if layer == "canopy_cover":
            return {"percent": value}

        return value

    def _aspect_to_direction(self, degrees: int) -> str:
        """Convert aspect degrees to cardinal direction."""
        if degrees < 0:
            return "flat"
        directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW", "N"]
        idx = int((degrees + 22.5) / 45)
        return directions[idx]

    @property
    def available_layers(self) -> list[str]:
        """List of registered layer names."""
        return list(self._layer_urls.keys())


# Singleton instance
_terrain_service: TerrainService | None = None


def get_terrain_service() -> TerrainService | None:
    """Get terrain service singleton (if configured)."""
    global _terrain_service

    if _terrain_service is not None:
        return _terrain_service

    if not settings.landfire_s3_prefix:
        logger.warning("LANDFIRE_S3_PREFIX not configured - terrain service unavailable")
        return None

    _terrain_service = TerrainService(settings.landfire_s3_prefix)

    # Auto-register known files based on legacy config or defaults
    # In production, you'd call discover_layers() with S3 listing
    if settings.landfire_cog_url:
        # Extract filename from legacy URL
        filename = settings.landfire_cog_url.split("/")[-1]
        _terrain_service.register_layer("fuel", filename)

    return _terrain_service
