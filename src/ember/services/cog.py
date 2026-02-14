"""Cloud Optimized GeoTIFF (COG) service for raster data queries.

Enables efficient point queries against large raster datasets stored in S3
using HTTP range requests. No need to download entire files.

Supports:
- S3 URLs (s3://bucket/path.tif) - requires AWS credentials
- HTTPS URLs (https://bucket.s3.region.amazonaws.com/path.tif)
- Local files (/path/to/file.tif)
"""

import os
from functools import lru_cache
from typing import Any

import numpy as np
from pyproj import Transformer
from rio_tiler.io import Reader

from ember.config import settings
from ember.logging import get_logger

logger = get_logger(__name__)


def _configure_aws_env() -> None:
    """Configure AWS credentials in environment for GDAL/rasterio."""
    if settings.aws_access_key_id:
        os.environ.setdefault("AWS_ACCESS_KEY_ID", settings.aws_access_key_id)
    if settings.aws_secret_access_key:
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", settings.aws_secret_access_key)
    if settings.aws_region:
        os.environ.setdefault("AWS_REGION", settings.aws_region)

    # GDAL configuration for efficient COG reading
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.tiff,.TIF,.TIFF")
    os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
    os.environ.setdefault("GDAL_HTTP_MULTIPLEX", "YES")
    os.environ.setdefault("GDAL_HTTP_VERSION", "2")
    os.environ.setdefault("VSI_CACHE", "TRUE")
    os.environ.setdefault("VSI_CACHE_SIZE", "5000000")  # 5MB cache


# Configure on module load
_configure_aws_env()


class COGService:
    """Service for querying Cloud Optimized GeoTIFFs."""

    def __init__(self, cog_url: str):
        """
        Initialize COG service.

        Args:
            cog_url: URL to COG file (s3://, https://, or local path)
        """
        self.cog_url = cog_url
        self._validate_url()

    def _validate_url(self) -> None:
        """Validate COG URL format."""
        if not self.cog_url:
            raise ValueError("COG URL is required")

        valid_prefixes = ("s3://", "https://", "http://", "/")
        if not any(self.cog_url.startswith(p) for p in valid_prefixes):
            raise ValueError(
                f"Invalid COG URL: {self.cog_url}. "
                f"Must start with one of: {valid_prefixes}"
            )

    def point_query(
        self,
        lat: float,
        lon: float,
        band: int = 1,
    ) -> dict[str, Any]:
        """
        Query raster value at a single point.

        Uses rio-tiler for efficient point queries with HTTP range requests.
        Only fetches the specific tile containing the point.

        Args:
            lat: Latitude (WGS84)
            lon: Longitude (WGS84)
            band: Band index (1-based, default 1)

        Returns:
            Dict with pixel value and metadata
        """
        try:
            with Reader(self.cog_url) as src:
                # Get raster info
                info = src.info()
                crs = info.crs

                # Transform coordinates if needed
                if crs and str(crs) != "EPSG:4326":
                    transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
                    x, y = transformer.transform(lon, lat)
                else:
                    x, y = lon, lat

                # Check bounds - bounds is tuple (left, bottom, right, top)
                bounds = info.bounds
                left, bottom, right, top = bounds[0], bounds[1], bounds[2], bounds[3]
                if not (left <= x <= right and bottom <= y <= top):
                    return {
                        "status": "out_of_bounds",
                        "lat": lat,
                        "lon": lon,
                        "message": "Coordinates outside raster extent",
                    }

                # Point query - fetches only the required tile
                point_data = src.point(lon, lat)

                # Extract value for requested band
                values = point_data.data
                if band > len(values):
                    return {
                        "status": "error",
                        "lat": lat,
                        "lon": lon,
                        "message": f"Band {band} not found (raster has {len(values)} bands)",
                    }

                value = values[band - 1]

                # Handle nodata
                nodata = getattr(info, "nodata", None)
                if nodata is not None and value == nodata:
                    return {
                        "status": "nodata",
                        "lat": lat,
                        "lon": lon,
                        "message": "No data at this location",
                    }

                # Convert numpy types to native Python
                if isinstance(value, (np.integer, np.floating)):
                    value = value.item()

                return {
                    "status": "success",
                    "lat": lat,
                    "lon": lon,
                    "value": value,
                    "band": band,
                    "crs": str(crs) if crs else None,
                }

        except Exception as e:
            logger.error(f"COG point query failed: {e}", exc_info=True)
            return {
                "status": "error",
                "lat": lat,
                "lon": lon,
                "message": f"Query failed: {str(e)}",
            }

    def get_info(self) -> dict[str, Any]:
        """
        Get COG metadata.

        Returns:
            Dict with bounds, CRS, resolution, band count, etc.
        """
        try:
            with Reader(self.cog_url) as src:
                info = src.info()

                bounds = info.bounds
                return {
                    "status": "success",
                    "url": self.cog_url,
                    "bounds": {
                        "left": bounds[0],
                        "bottom": bounds[1],
                        "right": bounds[2],
                        "top": bounds[3],
                    },
                    "crs": str(info.crs) if info.crs else None,
                    "width": getattr(info, "width", None),
                    "height": getattr(info, "height", None),
                    "band_count": getattr(info, "count", None),
                    "dtype": getattr(info, "dtype", None),
                    "nodata": getattr(info, "nodata", None),
                }

        except Exception as e:
            logger.error(f"COG info query failed: {e}", exc_info=True)
            return {
                "status": "error",
                "url": self.cog_url,
                "message": f"Failed to read COG info: {str(e)}",
            }


@lru_cache(maxsize=8)
def get_cog_service(cog_url: str) -> COGService:
    """Get cached COG service instance for a URL."""
    return COGService(cog_url)


# Pre-configured service for LANDFIRE (if URL configured)
def get_landfire_cog_service() -> COGService | None:
    """Get LANDFIRE COG service if configured."""
    if settings.landfire_cog_url:
        return get_cog_service(settings.landfire_cog_url)
    return None
