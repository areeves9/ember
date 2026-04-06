"""Copernicus Data Space service for Sentinel-2 products.

Generic evalscript pipeline supporting vegetation indices (NDVI/NDMI),
true-color imagery, and future Sentinel-2 products. Each product is
defined as a SentinelProduct dataclass and processed through a shared
pipeline: validation, bbox computation, resolution selection, caching,
Copernicus Process API call, rasterio parsing, and base64 encoding.
"""

import base64
import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import time
from typing import Any

import httpx
import numpy as np
import rasterio

from ember.config import settings
from ember.logging import get_logger

logger = get_logger(__name__)

# Cache for product queries
_product_cache: dict[str, dict] = {}
_CACHE_TTL_STATS = 21600  # 6 hours for stats
_CACHE_TTL_RASTER = 3600  # 1 hour for rasters
_CACHE_MAX_SIZE = 500

# Keep module-level alias so existing imports still work
_vegetation_cache = _product_cache

# Resolution tiers: area_km2 thresholds -> (width, height)
_RESOLUTION_TIERS = {
    "standard": [(10, 128), (50, 256), (float("inf"), 512)],
    "high": [(10, 256), (50, 512), (float("inf"), 1024)],
}


@dataclass(frozen=True)
class SentinelProduct:
    """Definition of a Sentinel-2 product processed through the generic pipeline."""

    name: str
    evalscript: str
    bands: int  # Output band count (1 or 3)
    default_format: str  # "stats" for indices, "png" for imagery
    resolution_tier: str  # "standard" or "high"
    interpret: Callable[[dict[str, float]], dict[str, Any]] | None = None


# --- Product Definitions ---

NDVI_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["B04", "B08"],
    output: { bands: 1, sampleType: "FLOAT32" }
  };
}
function evaluatePixel(sample) {
  let ndvi = (sample.B08 - sample.B04) / (sample.B08 + sample.B04);
  return [ndvi];
}
"""

NDMI_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["B08", "B11"],
    output: { bands: 1, sampleType: "FLOAT32" }
  };
}
function evaluatePixel(sample) {
  let ndmi = (sample.B08 - sample.B11) / (sample.B08 + sample.B11);
  return [ndmi];
}
"""

TRUECOLOR_EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: ["B04", "B03", "B02"],
    output: { bands: 3, sampleType: "AUTO" }
  };
}
function evaluatePixel(sample) {
  return [sample.B04 * 2.5, sample.B03 * 2.5, sample.B02 * 2.5];
}
"""


def _ndvi_interpret(stats: dict[str, float]) -> dict[str, Any]:
    """NDVI interpretation: vegetation status from mean value."""
    mean = stats["mean"]
    if mean < 0.1:
        status = "Bare/Barren"
    elif mean < 0.2:
        status = "Sparse Vegetation"
    elif mean < 0.4:
        status = "Moderate Vegetation"
    elif mean < 0.6:
        status = "Healthy Vegetation"
    else:
        status = "Dense Vegetation"
    return {"vegetation_status": status}


def _ndmi_interpret(stats: dict[str, float]) -> dict[str, Any]:
    """NDMI interpretation: moisture status and fire risk from mean value."""
    mean = stats["mean"]

    if mean < -0.2:
        moisture = "Very Dry"
    elif mean < 0.0:
        moisture = "Dry"
    elif mean < 0.2:
        moisture = "Moderate"
    elif mean < 0.4:
        moisture = "Moist"
    else:
        moisture = "Saturated"

    if mean < -0.1:
        risk = "High"
    elif mean < 0.1:
        risk = "Moderate"
    else:
        risk = "Low"

    return {"moisture_status": moisture, "fire_risk": risk}


PRODUCT_NDVI = SentinelProduct(
    name="ndvi",
    evalscript=NDVI_EVALSCRIPT,
    bands=1,
    default_format="stats",
    resolution_tier="standard",
    interpret=_ndvi_interpret,
)

PRODUCT_NDMI = SentinelProduct(
    name="ndmi",
    evalscript=NDMI_EVALSCRIPT,
    bands=1,
    default_format="stats",
    resolution_tier="standard",
    interpret=_ndmi_interpret,
)

PRODUCT_TRUECOLOR = SentinelProduct(
    name="truecolor",
    evalscript=TRUECOLOR_EVALSCRIPT,
    bands=3,
    default_format="png",
    resolution_tier="high",
    interpret=None,
)


class CopernicusService:
    """Service for fetching Sentinel-2 products from Copernicus Data Space."""

    def __init__(self):
        self.client_id = settings.copernicus_client_id
        self.client_secret = settings.copernicus_client_secret
        self.timeout = settings.http_timeout
        self.base_url = "https://sh.dataspace.copernicus.eu"
        self.process_endpoint = "/api/v1/process"
        self.token_url = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
        self._token: str | None = None
        self._token_expiry: datetime | None = None

    async def _get_token(self) -> str:
        """Get OAuth2 token from Copernicus Data Space."""
        if not self.client_id or not self.client_secret:
            raise ValueError("Copernicus credentials not configured")

        if self._token and self._token_expiry and self._token_expiry > datetime.now(timezone.utc):
            return self._token

        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()

        data = response.json()
        self._token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        self._token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)

        return self._token

    def _compute_resolution(self, area_km2: float, tier: str = "standard") -> tuple[int, int]:
        """Compute output resolution based on area size and tier."""
        thresholds = _RESOLUTION_TIERS.get(tier, _RESOLUTION_TIERS["standard"])
        for max_area, size in thresholds:
            if area_km2 < max_area:
                return size, size
        return thresholds[-1][1], thresholds[-1][1]

    def _compute_bbox_area_km2(
        self, min_lat: float, max_lat: float, min_lon: float, max_lon: float
    ) -> float:
        """Approximate bbox area in square kilometers."""
        lat_center = (min_lat + max_lat) / 2
        lon_width = max_lon - min_lon
        lat_height = max_lat - min_lat

        km_per_degree_lat = 111.32
        km_per_degree_lon = 111.32 * abs(np.cos(np.radians(lat_center)))

        return lon_width * km_per_degree_lon * lat_height * km_per_degree_lat

    async def _call_process_api(
        self,
        bbox: list[float],
        start_date: str,
        end_date: str,
        evalscript: str,
        width: int,
        height: int,
        output_format: str = "image/tiff",
    ) -> bytes:
        """Call Copernicus Sentinel Hub Process API."""
        token = await self._get_token()

        payload = {
            "input": {
                "bounds": {"bbox": bbox},
                "data": [
                    {
                        "type": "sentinel-2-l2a",
                        "dataFilter": {
                            "timeRange": {
                                "from": f"{start_date}T00:00:00Z",
                                "to": f"{end_date}T23:59:59Z",
                            }
                        },
                    }
                ],
            },
            "output": {
                "width": width,
                "height": height,
                "responses": [{"identifier": "default", "format": {"type": output_format}}],
            },
            "evalscript": evalscript,
        }

        url = f"{self.base_url}{self.process_endpoint}"

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()

        return response.content

    def _compute_stats_from_raster(self, raster_data: np.ndarray) -> dict[str, float]:
        """Compute statistics from raster data."""
        valid = raster_data[np.isfinite(raster_data)]
        if valid.size == 0:
            raise ValueError("No valid pixels found in raster")

        return {
            "mean": float(np.mean(valid)),
            "min": float(np.min(valid)),
            "max": float(np.max(valid)),
        }

    # ------------------------------------------------------------------
    # Generic product pipeline
    # ------------------------------------------------------------------

    async def _get_product(
        self,
        product: SentinelProduct,
        *,
        lat: float | None = None,
        lon: float | None = None,
        size_km: float = 5.0,
        min_lat: float | None = None,
        max_lat: float | None = None,
        min_lon: float | None = None,
        max_lon: float | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        format: str | None = None,
    ) -> dict[str, Any]:
        """
        Generic pipeline for any Sentinel-2 product.

        Handles: credential check, parameter validation, bbox computation,
        resolution selection, caching, Copernicus API call, rasterio parsing,
        base64 encoding, and response building.
        """
        if format is None:
            format = product.default_format

        # --- Credential check ---
        if not self.client_id or not self.client_secret:
            return {
                "status": "not_configured",
                "message": "Copernicus credentials not configured",
            }

        # --- Format validation ---
        if format not in ("stats", "raster", "png"):
            return {
                "status": "error",
                "message": "Invalid format. Must be 'stats', 'raster', or 'png'",
            }

        # --- Parameter validation ---
        if (lat is None) != (lon is None):
            return {
                "status": "error",
                "message": "Both lat and lon must be provided together",
            }

        if (min_lat is None) != (max_lat is None) or (min_lon is None) != (max_lon is None):
            return {
                "status": "error",
                "message": (
                    "All four bbox parameters (min_lat, max_lat, min_lon,"
                    " max_lon) must be provided together"
                ),
            }

        if lat is not None and (min_lat is not None or size_km <= 0 or size_km > 100):
            return {
                "status": "error",
                "message": "size_km must be between 1 and 100 when using lat/lon mode",
            }

        if min_lat is not None and max_lat is not None and min_lat >= max_lat:
            return {
                "status": "error",
                "message": "min_lat must be less than max_lat",
            }

        if min_lon is not None and max_lon is not None and min_lon >= max_lon:
            return {
                "status": "error",
                "message": "min_lon must be less than max_lon",
            }

        if start_date is not None and not isinstance(start_date, str):
            return {
                "status": "error",
                "message": "start_date must be a string in YYYY-MM-DD format",
            }

        if end_date is not None and not isinstance(end_date, str):
            return {
                "status": "error",
                "message": "end_date must be a string in YYYY-MM-DD format",
            }

        # --- Default dates ---
        if start_date is None:
            start_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        if end_date is None:
            end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # --- Bbox computation ---
        if lat is not None and lon is not None:
            km_per_degree_lat = 111.32
            km_per_degree_lon = 111.32 * abs(np.cos(np.radians(lat)))

            if km_per_degree_lon < 1e-6:
                return {
                    "status": "error",
                    "message": "Latitude too close to poles for bbox computation",
                }

            lat_radius = size_km / 2 / km_per_degree_lat
            lon_radius = size_km / 2 / km_per_degree_lon

            min_lat = lat - lat_radius
            max_lat = lat + lat_radius
            min_lon = lon - lon_radius
            max_lon = lon + lon_radius

        if min_lat is None or max_lat is None or min_lon is None or max_lon is None:
            return {
                "status": "error",
                "message": "Could not determine bounding box",
            }

        # --- Resolution ---
        area_km2 = self._compute_bbox_area_km2(min_lat, max_lat, min_lon, max_lon)
        width, height = self._compute_resolution(area_km2, product.resolution_tier)

        bbox = [min_lon, min_lat, max_lon, max_lat]

        # --- Cache check ---
        cache_key = (
            f"{product.name}:{format}"
            f":{min_lat:.4f},{max_lat:.4f},{min_lon:.4f},{max_lon:.4f}"
            f":{start_date}:{end_date}"
        )

        cached = _product_cache.get(cache_key)
        if cached:
            ttl = _CACHE_TTL_STATS if format == "stats" else _CACHE_TTL_RASTER
            if time() - cached["timestamp"] < ttl:
                logger.debug(f"Cache hit for {product.name}: {cache_key}")
                return cached["data"]

        # --- API call ---
        try:
            output_format = "image/png" if format == "png" else "image/tiff"

            image_data = await self._call_process_api(
                bbox,
                start_date,
                end_date,
                product.evalscript,
                width,
                height,
                output_format,
            )

            # --- Parse and build response ---
            result = self._build_response(
                product=product,
                format=format,
                image_data=image_data,
                bbox=bbox,
                width=width,
                height=height,
                start_date=start_date,
                end_date=end_date,
                lat=lat,
                lon=lon,
            )

            # --- Cache ---
            if len(_product_cache) >= _CACHE_MAX_SIZE:
                _product_cache.clear()
            _product_cache[cache_key] = {"timestamp": time(), "data": result}

            return result

        except httpx.HTTPStatusError as e:
            logger.error(f"Copernicus API error: {e}")
            return {
                "status": "error",
                "message": f"Copernicus API error: {str(e)}",
                "details": str(e) if hasattr(e, "response") else None,
            }
        except Exception as e:
            logger.error(f"Error computing {product.name}: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"Error computing {product.name}: {str(e)}",
            }

    def _build_response(
        self,
        *,
        product: SentinelProduct,
        format: str,
        image_data: bytes,
        bbox: list[float],
        width: int,
        height: int,
        start_date: str,
        end_date: str,
        lat: float | None,
        lon: float | None,
    ) -> dict[str, Any]:
        """Build response dict from raw image data, varying by format."""
        # Compute stats for single-band products when needed
        stats = None
        if product.bands == 1 and format in ("stats", "raster"):
            with rasterio.open(io.BytesIO(image_data)) as src:
                raster_data = src.read(1)
            stats = self._compute_stats_from_raster(raster_data)

        base = {
            "status": "success",
            "bbox": bbox,
            "source": "Sentinel-2 L2A",
            "date_range": {"start": start_date, "end": end_date},
        }

        if format == "stats" and stats is not None:
            product_data = {
                "mean": round(stats["mean"], 3),
                "min": round(stats["min"], 3),
                "max": round(stats["max"], 3),
            }
            if product.interpret:
                product_data.update(product.interpret(stats))
            base[product.name] = product_data

        elif format == "raster":
            raster_b64 = base64.b64encode(image_data).decode("utf-8")
            base["raster"] = {
                "format": "image/tiff",
                "encoding": "base64",
                "data": raster_b64,
                "width": width,
                "height": height,
            }
            if stats is not None:
                product_data = {
                    "mean": round(stats["mean"], 3),
                    "min": round(stats["min"], 3),
                    "max": round(stats["max"], 3),
                }
                if product.interpret:
                    product_data.update(product.interpret(stats))
                base[product.name] = product_data

        else:  # png
            png_b64 = base64.b64encode(image_data).decode("utf-8")
            base["raster"] = {
                "format": "image/png",
                "encoding": "base64",
                "data": png_b64,
                "width": width,
                "height": height,
            }

        # Add product identifier for non-index products
        if product.name not in ("ndvi", "ndmi"):
            base["product"] = product.name

        if lat is not None and lon is not None:
            base["latitude"] = lat
            base["longitude"] = lon

        return base

    # ------------------------------------------------------------------
    # Public API — thin wrappers
    # ------------------------------------------------------------------

    async def get_ndvi(
        self,
        lat: float | None = None,
        lon: float | None = None,
        size_km: float = 5.0,
        min_lat: float | None = None,
        max_lat: float | None = None,
        min_lon: float | None = None,
        max_lon: float | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        format: str = "stats",
    ) -> dict[str, Any]:
        """Get NDVI (Normalized Difference Vegetation Index) for a location.

        NDVI = (NIR - Red) / (NIR + Red)
        Range: -1 to +1 (higher = more vegetation)
        """
        return await self._get_product(
            PRODUCT_NDVI,
            lat=lat,
            lon=lon,
            size_km=size_km,
            min_lat=min_lat,
            max_lat=max_lat,
            min_lon=min_lon,
            max_lon=max_lon,
            start_date=start_date,
            end_date=end_date,
            format=format,
        )

    async def get_ndmi(
        self,
        lat: float | None = None,
        lon: float | None = None,
        size_km: float = 5.0,
        min_lat: float | None = None,
        max_lat: float | None = None,
        min_lon: float | None = None,
        max_lon: float | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        format: str = "stats",
    ) -> dict[str, Any]:
        """Get NDMI (Normalized Difference Moisture Index) for a location.

        NDMI = (NIR - SWIR) / (NIR + SWIR)
        Range: -1 to +1 (higher = more moisture)
        """
        return await self._get_product(
            PRODUCT_NDMI,
            lat=lat,
            lon=lon,
            size_km=size_km,
            min_lat=min_lat,
            max_lat=max_lat,
            min_lon=min_lon,
            max_lon=max_lon,
            start_date=start_date,
            end_date=end_date,
            format=format,
        )

    async def get_truecolor(
        self,
        lat: float | None = None,
        lon: float | None = None,
        size_km: float = 5.0,
        min_lat: float | None = None,
        max_lat: float | None = None,
        min_lon: float | None = None,
        max_lon: float | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        format: str = "png",
    ) -> dict[str, Any]:
        """Get true-color RGB satellite imagery for a location.

        Combines Sentinel-2 B04 (Red), B03 (Green), B02 (Blue) bands
        with 2.5x gain to produce a natural-color photograph from space.
        Default format is PNG. Uses high resolution tier (256/512/1024).
        """
        return await self._get_product(
            PRODUCT_TRUECOLOR,
            lat=lat,
            lon=lon,
            size_km=size_km,
            min_lat=min_lat,
            max_lat=max_lat,
            min_lon=min_lon,
            max_lon=max_lon,
            start_date=start_date,
            end_date=end_date,
            format=format,
        )

    # Keep old classification methods accessible for tests
    def _ndvi_to_status(self, ndvi: float) -> str:
        """Convert NDVI value to vegetation status."""
        return _ndvi_interpret({"mean": ndvi})["vegetation_status"]

    def _ndmi_to_moisture_status(self, ndmi: float) -> str:
        """Convert NDMI value to moisture status."""
        return _ndmi_interpret({"mean": ndmi})["moisture_status"]

    def _ndmi_to_fire_risk(self, ndmi: float) -> str:
        """Convert NDMI value to fire risk level."""
        return _ndmi_interpret({"mean": ndmi})["fire_risk"]


copernicus_service = CopernicusService()
