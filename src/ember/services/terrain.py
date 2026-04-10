"""Terrain service for multi-layer LANDFIRE queries.

Queries multiple raster layers in parallel and returns combined terrain data
for fire behavior modeling.
"""

import asyncio
import base64
import io
import math
from concurrent.futures import ThreadPoolExecutor
from time import time
from typing import Any

import rasterio
from rio_tiler.io import Reader

from ember.config import settings
from ember.data import BPS_CODES, EVT_CODES, FDIST_CODES
from ember.logging import get_logger
from ember.services.cog import COGService, get_cog_service

logger = get_logger(__name__)

# Simple in-memory cache for terrain queries
# LANDFIRE data is static, so long TTL is fine
_terrain_cache: dict[str, dict] = {}
_CACHE_TTL_SECONDS = 1800  # 30 minutes
_CACHE_MAX_SIZE = 1000  # Max entries before purge

# Raster cache - separate from point cache due to larger payloads
_raster_cache: dict[str, dict] = {}
_RASTER_CACHE_TTL_SECONDS = 86400  # 24 hours (LANDFIRE data is static)
_RASTER_CACHE_MAX_SIZE = 100  # Fewer entries (larger payloads)
_RASTER_CACHE_MAX_MEMORY_MB = 500  # Approximate memory limit

# Layer name -> file pattern mapping
# Pattern matches: LC{YY}_{PATTERN}_{RES}.tif
LAYER_PATTERNS = {
    # Existing — Topographic (2020)
    "slope": "SlpD",  # Slope in degrees (0-90)
    "aspect": "Asp",  # Aspect in degrees (0-360, -1 = flat)
    "elevation": "Elev",  # Elevation in meters
    # Existing — Canopy (2024)
    "canopy_height": "CH",  # Canopy height in meters * 10
    "canopy_base_height": "CBH",  # Canopy base height in meters * 10
    "canopy_bulk_density": "CBD",  # Canopy bulk density kg/m³ * 100
    "canopy_cover": "CC",  # Canopy cover percent (0-100)
    # Existing — Fuel (2024)
    "fuel": "F40",  # FBFM40 fuel model (91-204 -> NB1, GR2, SH5, etc.)
    # New — Fuel (2024)
    "fuel_model_13": "FBFM13",  # Anderson 13 fuel model (1-13)
    # New — Vegetation (2024/2020)
    "vegetation_type": "EVT",  # Existing vegetation type (categorical)
    "vegetation_cover": "EVC",  # Existing vegetation cover (percent)
    "vegetation_height": "EVH",  # Existing vegetation height (meters * 10)
    "biophysical_settings": "BPS",  # Pre-settlement vegetation (categorical)
    # New — Fire Regime (2016/2024)
    "fire_regime_group": "FRG",  # Fire regime group (1-5)
    "fire_return_interval": "FRI",  # Mean fire return interval (years)
    "percent_fire_severity": "PFS",  # Percent high-severity fire
    "vegetation_departure": "VDep",  # Departure from historical (0-100%)
    "vegetation_condition": "VCC",  # Condition class (1-3)
    "succession_classes": "SClass",  # Succession class (A-E + special)
    # New — Disturbance (2024)
    "fuel_disturbance": "FDist",  # Recent fuel disturbance (categorical)
}

# Layers that use categorical pixel values (need nearest-neighbor resampling)
CATEGORICAL_LAYERS = {
    "fuel",
    "fuel_model_13",
    "vegetation_type",
    "biophysical_settings",
    "fire_regime_group",
    "vegetation_condition",
    "succession_classes",
    "fuel_disturbance",
}

# FBFM40 pixel value -> fuel code mapping
FUEL_CODES = {
    91: "NB1",
    92: "NB2",
    93: "NB3",
    98: "NB8",
    99: "NB9",
    101: "GR1",
    102: "GR2",
    103: "GR3",
    104: "GR4",
    105: "GR5",
    106: "GR6",
    107: "GR7",
    108: "GR8",
    109: "GR9",
    121: "GS1",
    122: "GS2",
    123: "GS3",
    124: "GS4",
    141: "SH1",
    142: "SH2",
    143: "SH3",
    144: "SH4",
    145: "SH5",
    146: "SH6",
    147: "SH7",
    148: "SH8",
    149: "SH9",
    161: "TU1",
    162: "TU2",
    163: "TU3",
    164: "TU4",
    165: "TU5",
    181: "TL1",
    182: "TL2",
    183: "TL3",
    184: "TL4",
    185: "TL5",
    186: "TL6",
    187: "TL7",
    188: "TL8",
    189: "TL9",
    201: "SB1",
    202: "SB2",
    203: "SB3",
    204: "SB4",
}

# Anderson 13 fuel model pixel value -> code mapping
ANDERSON_13_CODES = {
    1: "1 - Short Grass",
    2: "2 - Timber Grass/Understory",
    3: "3 - Tall Grass",
    4: "4 - Chaparral",
    5: "5 - Brush",
    6: "6 - Dormant Brush/Hardwood Slash",
    7: "7 - Southern Rough",
    8: "8 - Closed Timber Litter",
    9: "9 - Hardwood Litter",
    10: "10 - Timber Litter/Understory",
    11: "11 - Light Logging Slash",
    12: "12 - Medium Logging Slash",
    13: "13 - Heavy Logging Slash",
    91: "Urban/Developed",
    92: "Snow/Ice",
    93: "Agriculture",
    98: "Water",
    99: "Barren",
}

# Fire Regime Group pixel value -> label
FIRE_REGIME_GROUPS = {
    1: "I - Frequent low-severity (0-35 yr)",
    2: "II - Frequent replacement (0-35 yr)",
    3: "III - Mixed severity (35-200 yr)",
    4: "IV - Replacement severity (35-200 yr)",
    5: "V - Very rare replacement (200+ yr)",
    111: "Water",
    112: "Snow/Ice",
    120: "Developed",
    131: "Barren",
    132: "Sparse",
    180: "Agriculture",
}

# Vegetation Condition Class pixel value -> label
VEGETATION_CONDITION_CLASSES = {
    1: "Within historical range",
    2: "Moderately departed",
    3: "Significantly departed",
    111: "Water",
    112: "Snow/Ice",
    120: "Developed",
    131: "Barren",
    132: "Sparse",
    180: "Agriculture",
}

# Succession Class pixel value -> label
SUCCESSION_CLASSES = {
    1: "A - Early succession",
    2: "B - Mid succession (open)",
    3: "C - Mid succession (closed)",
    4: "D - Late succession (open)",
    5: "E - Late succession (closed)",
    6: "UN - Uncharacteristic native",
    7: "UE - Uncharacteristic exotic",
    111: "Water",
    112: "Snow/Ice",
    120: "Developed",
    132: "Barren/Sparse",
    180: "Agriculture",
}


def _raster_cache_key(
    layer: str,
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    max_size: int,
) -> str:
    """
    Generate cache key for raster query.

    Rounds coordinates to 3 decimals (~110m) to improve cache hits
    when viewport shifts slightly.
    """
    r = lambda x: round(x, 3)
    return f"raster:{layer}:{r(min_lat)},{r(max_lat)},{r(min_lon)},{r(max_lon)}:{max_size}"


def _get_cached_raster(key: str) -> dict | None:
    """Get raster from cache if still valid."""
    entry = _raster_cache.get(key)
    if not entry:
        return None

    if time() - entry["timestamp"] > _RASTER_CACHE_TTL_SECONDS:
        del _raster_cache[key]
        return None

    return entry["data"]


def _cache_raster(key: str, data: dict) -> None:
    """Store raster in cache with size management."""
    # Simple size management: clear if too many entries
    if len(_raster_cache) >= _RASTER_CACHE_MAX_SIZE:
        # Remove oldest entries (simple LRU approximation)
        sorted_entries = sorted(_raster_cache.items(), key=lambda x: x[1]["timestamp"])
        # Remove oldest 20%
        for key_to_remove, _ in sorted_entries[: len(sorted_entries) // 5]:
            del _raster_cache[key_to_remove]

    _raster_cache[key] = {
        "timestamp": time(),
        "data": data,
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

        # New — Fuel
        if layer == "fuel_model_13":
            code = ANDERSON_13_CODES.get(value, f"Unknown({value})")
            return {"code": code, "raw": value}

        # New — Vegetation
        if layer == "vegetation_type":
            name = EVT_CODES.get(str(value), f"Unknown({value})")
            return {"name": name, "raw": value}

        if layer == "vegetation_cover":
            return {"percent": value}

        if layer == "vegetation_height":
            # Stored as meters * 10 (same as canopy height)
            return {"meters": value / 10.0 if value else None}

        if layer == "biophysical_settings":
            name = BPS_CODES.get(str(value), f"Unknown({value})")
            return {"name": name, "raw": value}

        # New — Fire Regime
        if layer == "fire_regime_group":
            label = FIRE_REGIME_GROUPS.get(value, f"Unknown({value})")
            return {"group": label, "raw": value}

        if layer == "fire_return_interval":
            return {"years": value}

        if layer == "percent_fire_severity":
            return {"percent": value}

        if layer == "vegetation_departure":
            return {"percent": value}

        if layer == "vegetation_condition":
            label = VEGETATION_CONDITION_CLASSES.get(value, f"Unknown({value})")
            return {"class": label, "raw": value}

        if layer == "succession_classes":
            label = SUCCESSION_CLASSES.get(value, f"Unknown({value})")
            return {"class": label, "raw": value}

        # New — Disturbance
        if layer == "fuel_disturbance":
            info = FDIST_CODES.get(str(value))
            if info:
                return {
                    "type": info["type"],
                    "severity": info["severity"],
                    "time": info["time"],
                    "raw": value,
                }
            return {"type": f"Unknown({value})", "raw": value}

        return value

    def _aspect_to_direction(self, degrees: int) -> str:
        """Convert aspect degrees to cardinal direction."""
        if degrees < 0:
            return "flat"
        directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW", "N"]
        idx = int((degrees + 22.5) / 45)
        return directions[idx]

    async def query_terrain_bbox_raster(
        self,
        min_lat: float,
        max_lat: float,
        min_lon: float,
        max_lon: float,
        layer: str,
        max_size: int = 512,
    ) -> dict[str, Any]:
        """
        Query terrain layer as a raster for a bounding box.
        Results are cached for 24 hours.

        Args:
            min_lat: South boundary
            max_lat: North boundary
            min_lon: West boundary
            max_lon: East boundary
            layer: Layer name (elevation, slope, aspect, fuel, etc.)
            max_size: Maximum dimension for output raster (default 512px)

        Returns:
            Dict with base64-encoded GeoTIFF and metadata
        """
        # Validate max_size parameter
        if max_size <= 0 or max_size > 2048:
            return {
                "status": "error",
                "message": "max_size must be between 1 and 2048",
            }

        # Validate coordinate ranges
        if not (-90 <= min_lat <= max_lat <= 90):
            return {
                "status": "error",
                "message": "Latitude values must be between -90 and 90",
            }

        if not (-180 <= min_lon <= max_lon <= 180):
            return {
                "status": "error",
                "message": "Longitude values must be between -180 and 180",
            }

        # Validate bbox coordinates
        if min_lat >= max_lat or min_lon >= max_lon:
            return {
                "status": "error",
                "message": "Invalid bbox: min values must be less than max values",
            }

        # Check bbox size to prevent memory issues
        if (max_lat - min_lat) > 10 or (max_lon - min_lon) > 10:
            return {
                "status": "error",
                "message": "Bbox too large (max 10 degrees per dimension)",
            }

        # Minimum bbox clamp (1km) — prevents upscaling beyond native
        # resolution (LANDFIRE is 30m, so <1km bbox yields <33 native pixels)
        min_lat_span_deg = 1.0 / 111.32  # ~1km in degrees latitude
        lat_center = (min_lat + max_lat) / 2
        km_per_deg_lon = 111.32 * abs(math.cos(math.radians(lat_center)))
        min_lon_span_deg = 1.0 / km_per_deg_lon if km_per_deg_lon > 1e-6 else min_lat_span_deg

        if (max_lat - min_lat) < min_lat_span_deg:
            pad = (min_lat_span_deg - (max_lat - min_lat)) / 2
            min_lat -= pad
            max_lat += pad
        if (max_lon - min_lon) < min_lon_span_deg:
            pad = (min_lon_span_deg - (max_lon - min_lon)) / 2
            min_lon -= pad
            max_lon += pad

        # Clamp to valid coordinate ranges after expansion
        min_lat = max(min_lat, -90.0)
        max_lat = min(max_lat, 90.0)
        min_lon = max(min_lon, -180.0)
        max_lon = min(max_lon, 180.0)

        if layer not in self._layer_urls:
            return {
                "status": "error",
                "message": f"Layer '{layer}' not available. Available: {self.available_layers}",
            }

        url = self._layer_urls[layer]

        # Check cache first
        cache_key = _raster_cache_key(layer, min_lat, max_lat, min_lon, max_lon, max_size)
        cached = _get_cached_raster(cache_key)
        if cached:
            logger.debug(f"Raster cache hit: {cache_key}")
            return cached

        logger.debug(f"Raster cache miss: {cache_key}")

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                self._executor,
                self._read_bbox_raster,
                url,
                min_lat,
                max_lat,
                min_lon,
                max_lon,
                layer,
                max_size,
            )

            # Cache successful results
            if result.get("status") == "success":
                _cache_raster(cache_key, result)

            return result
        except Exception as e:
            logger.error(f"Bbox raster query failed for {layer}: {e}", exc_info=True)
            return {
                "status": "error",
                "message": "Failed to query terrain raster. Please check coordinates and try again.",
            }

    def _read_bbox_raster(
        self,
        url: str,
        min_lat: float,
        max_lat: float,
        min_lon: float,
        max_lon: float,
        layer: str,
        max_size: int,
    ) -> dict[str, Any]:
        """
        Read raster data for bbox (runs in thread pool).

        Uses rio-tiler's Reader for efficient COG reading with
        HTTP range requests.
        """
        with Reader(url) as src:
            # Get raster bounds and check intersection
            info = src.info()

            # Read the part of the raster that intersects our bbox
            # rio-tiler handles coordinate transformation automatically
            # bbox format: (left, bottom, right, top) = (min_lon, min_lat, max_lon, max_lat)

            # Use nearest neighbor for categorical data (fuel codes, vegetation types, etc.)
            # Bilinear interpolation would create invalid intermediate values
            resampling_method = "nearest" if layer in CATEGORICAL_LAYERS else "bilinear"

            img = src.part(
                bbox=(min_lon, min_lat, max_lon, max_lat),
                max_size=max_size,
                resampling_method=resampling_method,
            )

            # Get the data array (shape: bands x height x width)
            data = img.data

            if data.size == 0:
                return {
                    "status": "no_data",
                    "message": "No data in requested region",
                }

            # Create in-memory GeoTIFF
            buffer = io.BytesIO()

            # Get the actual bounds of the returned data
            actual_bounds = img.bounds

            # Calculate transform for the output raster
            height, width = data.shape[1], data.shape[2]
            transform = rasterio.transform.from_bounds(
                actual_bounds.left,
                actual_bounds.bottom,
                actual_bounds.right,
                actual_bounds.top,
                width,
                height,
            )

            # Write to in-memory GeoTIFF
            with rasterio.open(
                buffer,
                "w",
                driver="GTiff",
                height=height,
                width=width,
                count=1,
                dtype=data.dtype,
                crs="EPSG:4326",
                transform=transform,
                compress="lzw",
            ) as dst:
                dst.write(data[0], 1)  # Write first band

            # Encode as base64
            buffer.seek(0)
            b64_data = base64.b64encode(buffer.read()).decode("utf-8")

            # Get stats for the data
            # Initialize valid_data first
            valid_data = data.flatten()

            # Handle nodata/mask
            if hasattr(info, "nodata") and info.nodata is not None:
                valid_data = valid_data[valid_data != info.nodata]
            elif hasattr(data, "mask") and data.mask is not False:
                valid_data = data.compressed()

            # Return no_data status if all values are invalid/masked
            if valid_data.size == 0:
                return {
                    "status": "no_data",
                    "message": "No valid data in requested region",
                }

            return {
                "status": "success",
                "layer": layer,
                "bbox": [min_lon, min_lat, max_lon, max_lat],
                "raster": {
                    "format": "geotiff",
                    "encoding": "base64",
                    "data": b64_data,
                    "width": width,
                    "height": height,
                },
                "stats": {
                    "min": float(valid_data.min()),
                    "max": float(valid_data.max()),
                    "mean": float(valid_data.mean()),
                },
            }

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
