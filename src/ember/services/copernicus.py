"""Copernicus Data Space service for vegetation indices (NDVI/NDMI).

Full implementation using Copernicus Data Space Ecosystem API with OAuth2
client credentials flow. Provides both statistics and raster data for
vegetation analysis.
"""

import base64
import io
from datetime import datetime, timedelta, timezone
from time import time
from typing import Any

import httpx
import numpy as np
import rasterio

from ember.config import settings
from ember.logging import get_logger

logger = get_logger(__name__)

# Cache for vegetation queries
_vegetation_cache: dict[str, dict] = {}
_VEGETATION_CACHE_TTL_STATS = 21600  # 6 hours for stats
_VEGETATION_CACHE_TTL_RASTER = 3600  # 1 hour for rasters
_VEGETATION_CACHE_MAX_SIZE = 500  # Max entries before purge


class CopernicusService:
    """Service for fetching vegetation indices from Copernicus Sentinel-2 data."""

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

        # Return cached token if still valid
        if (
            self._token
            and self._token_expiry
            and self._token_expiry > datetime.now(timezone.utc)
        ):
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
        self._token_expiry = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in - 60
        )  # 60s buffer

        return self._token

    def _compute_resolution(self, area_km2: float) -> tuple[int, int]:
        """Compute output resolution based on area size."""
        if area_km2 < 10:
            return 128, 128
        elif area_km2 < 50:
            return 256, 256
        else:
            return 512, 512

    def _compute_bbox_area_km2(
        self, min_lat: float, max_lat: float, min_lon: float, max_lon: float
    ) -> float:
        """Approximate bbox area in square kilometers."""
        # Simple approximation using latitude scaling
        lat_center = (min_lat + max_lat) / 2
        lon_width = max_lon - min_lon
        lat_height = max_lat - min_lat

        # Approximate km per degree (scales with latitude)
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
                "responses": [
                    {"identifier": "default", "format": {"type": output_format}}
                ],
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
        """
        Get NDVI (Normalized Difference Vegetation Index) for a location.

        NDVI = (NIR - Red) / (NIR + Red)
        Range: -1 to +1 (higher = more vegetation)

        Args:
            lat: Center latitude (use with lon and size_km)
            lon: Center longitude (use with lat and size_km)
            size_km: Bounding box size in km (1-100, use with lat/lon)
            min_lat: South bound (use for bbox mode)
            max_lat: North bound (use for bbox mode)
            min_lon: West bound (use for bbox mode)
            max_lon: East bound (use for bbox mode)
            start_date: Start date YYYY-MM-DD (default: 7 days ago)
            end_date: End date YYYY-MM-DD (default: today)
            format: Response format: "stats" or "raster"

        Returns:
            Dict with NDVI statistics or raster data
        """
        # Validate input parameters
        if not self.client_id or not self.client_secret:
            return {
                "status": "not_configured",
                "message": "Copernicus credentials not configured",
            }

        # Validate format parameter
        if format not in ["stats", "raster", "png"]:
            return {
                "status": "error",
                "message": "Invalid format. Must be 'stats', 'raster', or 'png'",
            }

        # Validate parameter combinations
        if (lat is None) != (lon is None):
            return {
                "status": "error",
                "message": "Both lat and lon must be provided together",
            }

        if (min_lat is None) != (max_lat is None) or (min_lon is None) != (
            max_lon is None
        ):
            return {
                "status": "error",
                "message": "All four bbox parameters (min_lat, max_lat, min_lon, max_lon) must be provided together",
            }

        if lat is not None and (min_lat is not None or size_km <= 0 or size_km > 100):
            return {
                "status": "error",
                "message": "size_km must be between 1 and 100 when using lat/lon mode",
            }

        # Validate bbox coordinates if provided
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

        # Validate date parameters
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

        # Set default dates
        if start_date is None:
            start_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
                "%Y-%m-%d"
            )
        if end_date is None:
            end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Compute bounding box
        if lat is not None and lon is not None:
            # Convert lat/lon/size_km to bbox
            # Simple approximation - this could be enhanced with proper geodesic calculations
            km_per_degree_lat = 111.32
            km_per_degree_lon = 111.32 * abs(np.cos(np.radians(lat)))

            lat_radius = size_km / 2 / km_per_degree_lat
            lon_radius = size_km / 2 / km_per_degree_lon

            min_lat = lat - lat_radius
            max_lat = lat + lat_radius
            min_lon = lon - lon_radius
            max_lon = lon + lon_radius

        # Validate bbox
        if min_lat is None or max_lat is None or min_lon is None or max_lon is None:
            return {
                "status": "error",
                "message": "Could not determine bounding box",
            }

        # Compute area and resolution
        area_km2 = self._compute_bbox_area_km2(min_lat, max_lat, min_lon, max_lon)
        width, height = self._compute_resolution(area_km2)

        # Build bbox array for Sentinel Hub API
        bbox = [min_lon, min_lat, max_lon, max_lat]

        # Build cache key
        cache_key = f"ndvi:{format}:{min_lat:.4f},{max_lat:.4f},{min_lon:.4f},{max_lon:.4f}:{start_date}:{end_date}"

        # Check cache
        cached = _vegetation_cache.get(cache_key)
        if cached:
            ttl = (
                _VEGETATION_CACHE_TTL_STATS
                if format == "stats"
                else _VEGETATION_CACHE_TTL_RASTER
            )
            if time() - cached["timestamp"] < ttl:
                logger.debug(f"Cache hit for NDVI: {cache_key}")
                return cached["data"]

        # NDVI evalscript
        evalscript = """
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

        try:
            # Determine output format for Copernicus API
            output_format = "image/png" if format == "png" else "image/tiff"

            # Call Copernicus Process API
            image_data = await self._call_process_api(
                bbox, start_date, end_date, evalscript, width, height, output_format
            )

            # For stats, we need GeoTIFF - if PNG requested, fetch GeoTIFF separately
            if format == "stats" or format == "raster":
                # Parse GeoTIFF and compute statistics
                with rasterio.open(io.BytesIO(image_data)) as src:
                    raster_data = src.read(1)

                stats = self._compute_stats_from_raster(raster_data)

            if format == "stats":
                result = {
                    "status": "success",
                    "bbox": bbox,
                    "ndvi": {
                        "mean": round(stats["mean"], 3),
                        "min": round(stats["min"], 3),
                        "max": round(stats["max"], 3),
                        "vegetation_status": self._ndvi_to_status(stats["mean"]),
                    },
                    "source": "Sentinel-2 L2A",
                    "date_range": {
                        "start": start_date,
                        "end": end_date,
                    },
                }

                if lat is not None and lon is not None:
                    result["latitude"] = lat
                    result["longitude"] = lon

            elif format == "raster":  # GeoTIFF
                raster_b64 = base64.b64encode(image_data).decode("utf-8")

                result = {
                    "status": "success",
                    "bbox": bbox,
                    "raster": {
                        "format": "image/tiff",
                        "encoding": "base64",
                        "data": raster_b64,
                        "width": width,
                        "height": height,
                    },
                    "ndvi": {
                        "mean": round(stats["mean"], 3),
                        "min": round(stats["min"], 3),
                        "max": round(stats["max"], 3),
                    },
                    "source": "Sentinel-2 L2A",
                    "date_range": {
                        "start": start_date,
                        "end": end_date,
                    },
                }

                if lat is not None and lon is not None:
                    result["latitude"] = lat
                    result["longitude"] = lon

            else:  # format == "png"
                png_b64 = base64.b64encode(image_data).decode("utf-8")

                result = {
                    "status": "success",
                    "bbox": bbox,
                    "raster": {
                        "format": "image/png",
                        "encoding": "base64",
                        "data": png_b64,
                        "width": width,
                        "height": height,
                    },
                    "source": "Sentinel-2 L2A",
                    "date_range": {
                        "start": start_date,
                        "end": end_date,
                    },
                }

                if lat is not None and lon is not None:
                    result["latitude"] = lat
                    result["longitude"] = lon

            # Store in cache
            if len(_vegetation_cache) >= _VEGETATION_CACHE_MAX_SIZE:
                _vegetation_cache.clear()  # Simple purge when full
            _vegetation_cache[cache_key] = {"timestamp": time(), "data": result}

            return result

        except httpx.HTTPStatusError as e:
            logger.error(f"Copernicus API error: {e}")
            return {
                "status": "error",
                "message": f"Copernicus API error: {str(e)}",
                "details": str(e) if hasattr(e, "response") else None,
            }
        except Exception as e:
            logger.error(f"Error computing NDVI: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"Error computing NDVI: {str(e)}",
            }

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
        """
        Get NDMI (Normalized Difference Moisture Index) for a location.

        NDMI = (NIR - SWIR) / (NIR + SWIR)
        Range: -1 to +1 (higher = more moisture)

        Args:
            lat: Center latitude (use with lon and size_km)
            lon: Center longitude (use with lat and size_km)
            size_km: Bounding box size in km (1-100, use with lat/lon)
            min_lat: South bound (use for bbox mode)
            max_lat: North bound (use for bbox mode)
            min_lon: West bound (use for bbox mode)
            max_lon: East bound (use for bbox mode)
            start_date: Start date YYYY-MM-DD (default: 7 days ago)
            end_date: End date YYYY-MM-DD (default: today)
            format: Response format: "stats" or "raster"

        Returns:
            Dict with NDMI statistics, moisture status, and fire risk assessment
        """
        # Validate input parameters
        if not self.client_id or not self.client_secret:
            return {
                "status": "not_configured",
                "message": "Copernicus credentials not configured",
            }

        # Validate format parameter
        if format not in ["stats", "raster", "png"]:
            return {
                "status": "error",
                "message": "Invalid format. Must be 'stats', 'raster', or 'png'",
            }

        # Validate parameter combinations
        if (lat is None) != (lon is None):
            return {
                "status": "error",
                "message": "Both lat and lon must be provided together",
            }

        if (min_lat is None) != (max_lat is None) or (min_lon is None) != (
            max_lon is None
        ):
            return {
                "status": "error",
                "message": "All four bbox parameters (min_lat, max_lat, min_lon, max_lon) must be provided together",
            }

        if lat is not None and (min_lat is not None or size_km <= 0 or size_km > 100):
            return {
                "status": "error",
                "message": "size_km must be between 1 and 100 when using lat/lon mode",
            }

        # Validate bbox coordinates if provided
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

        # Validate date parameters
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

        # Set default dates
        if start_date is None:
            start_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
                "%Y-%m-%d"
            )
        if end_date is None:
            end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Compute bounding box
        if lat is not None and lon is not None:
            # Convert lat/lon/size_km to bbox
            km_per_degree_lat = 111.32
            km_per_degree_lon = 111.32 * abs(np.cos(np.radians(lat)))

            lat_radius = size_km / 2 / km_per_degree_lat
            lon_radius = size_km / 2 / km_per_degree_lon

            min_lat = lat - lat_radius
            max_lat = lat + lat_radius
            min_lon = lon - lon_radius
            max_lon = lon + lon_radius

        # Validate bbox
        if min_lat is None or max_lat is None or min_lon is None or max_lon is None:
            return {
                "status": "error",
                "message": "Could not determine bounding box",
            }

        # Compute area and resolution
        area_km2 = self._compute_bbox_area_km2(min_lat, max_lat, min_lon, max_lon)
        width, height = self._compute_resolution(area_km2)

        # Build bbox array for Sentinel Hub API
        bbox = [min_lon, min_lat, max_lon, max_lat]

        # Build cache key
        cache_key = f"ndmi:{format}:{min_lat:.4f},{max_lat:.4f},{min_lon:.4f},{max_lon:.4f}:{start_date}:{end_date}"

        # Check cache
        cached = _vegetation_cache.get(cache_key)
        if cached:
            ttl = (
                _VEGETATION_CACHE_TTL_STATS
                if format == "stats"
                else _VEGETATION_CACHE_TTL_RASTER
            )
            if time() - cached["timestamp"] < ttl:
                logger.debug(f"Cache hit for NDMI: {cache_key}")
                return cached["data"]

        # NDMI evalscript
        evalscript = """
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

        try:
            # Call Copernicus Process API
            geotiff_data = await self._call_process_api(
                bbox, start_date, end_date, evalscript, width, height
            )

            # Parse GeoTIFF and compute statistics
            with rasterio.open(io.BytesIO(geotiff_data)) as src:
                raster_data = src.read(1)

            stats = self._compute_stats_from_raster(raster_data)

            if format == "stats":
                result = {
                    "status": "success",
                    "bbox": bbox,
                    "ndmi": {
                        "mean": round(stats["mean"], 3),
                        "min": round(stats["min"], 3),
                        "max": round(stats["max"], 3),
                        "moisture_status": self._ndmi_to_moisture_status(stats["mean"]),
                        "fire_risk": self._ndmi_to_fire_risk(stats["mean"]),
                    },
                    "source": "Sentinel-2 L2A",
                    "date_range": {
                        "start": start_date,
                        "end": end_date,
                    },
                }

                if lat is not None and lon is not None:
                    result["latitude"] = lat
                    result["longitude"] = lon

            elif format == "raster":  # GeoTIFF
                raster_b64 = base64.b64encode(image_data).decode("utf-8")

                result = {
                    "status": "success",
                    "bbox": bbox,
                    "raster": {
                        "format": "image/tiff",
                        "encoding": "base64",
                        "data": raster_b64,
                        "width": width,
                        "height": height,
                    },
                    "ndmi": {
                        "mean": round(stats["mean"], 3),
                        "min": round(stats["min"], 3),
                        "max": round(stats["max"], 3),
                        "moisture_status": self._ndmi_to_moisture_status(stats["mean"]),
                        "fire_risk": self._ndmi_to_fire_risk(stats["mean"]),
                    },
                    "source": "Sentinel-2 L2A",
                    "date_range": {
                        "start": start_date,
                        "end": end_date,
                    },
                }

                if lat is not None and lon is not None:
                    result["latitude"] = lat
                    result["longitude"] = lon

            else:  # format == "png"
                png_b64 = base64.b64encode(image_data).decode("utf-8")

                result = {
                    "status": "success",
                    "bbox": bbox,
                    "raster": {
                        "format": "image/png",
                        "encoding": "base64",
                        "data": png_b64,
                        "width": width,
                        "height": height,
                    },
                    "source": "Sentinel-2 L2A",
                    "date_range": {
                        "start": start_date,
                        "end": end_date,
                    },
                }

                if lat is not None and lon is not None:
                    result["latitude"] = lat
                    result["longitude"] = lon

            # Store in cache
            if len(_vegetation_cache) >= _VEGETATION_CACHE_MAX_SIZE:
                _vegetation_cache.clear()  # Simple purge when full
            _vegetation_cache[cache_key] = {"timestamp": time(), "data": result}

            return result

        except httpx.HTTPStatusError as e:
            logger.error(f"Copernicus API error: {e}")
            return {
                "status": "error",
                "message": f"Copernicus API error: {str(e)}",
                "details": str(e) if hasattr(e, "response") else None,
            }
        except Exception as e:
            logger.error(f"Error computing NDMI: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"Error computing NDMI: {str(e)}",
            }

    def _ndvi_to_status(self, ndvi: float) -> str:
        """Convert NDVI value to vegetation status."""
        if ndvi < 0.1:
            return "Bare/Barren"
        elif ndvi < 0.2:
            return "Sparse Vegetation"
        elif ndvi < 0.4:
            return "Moderate Vegetation"
        elif ndvi < 0.6:
            return "Healthy Vegetation"
        else:
            return "Dense Vegetation"

    def _ndmi_to_moisture_status(self, ndmi: float) -> str:
        """Convert NDMI value to moisture status."""
        if ndmi < -0.2:
            return "Very Dry"
        elif ndmi < 0.0:
            return "Dry"
        elif ndmi < 0.2:
            return "Moderate"
        elif ndmi < 0.4:
            return "Moist"
        else:
            return "Saturated"

    def _ndmi_to_fire_risk(self, ndmi: float) -> str:
        """Convert NDMI value to fire risk level."""
        if ndmi < -0.1:
            return "High"
        elif ndmi < 0.1:
            return "Moderate"
        else:
            return "Low"


copernicus_service = CopernicusService()
