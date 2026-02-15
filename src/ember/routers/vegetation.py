"""Vegetation index endpoints - Copernicus Sentinel-2."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from ember.auth import require_auth
from ember.services.copernicus import copernicus_service

router = APIRouter(prefix="/vegetation", tags=["vegetation"])


@router.get("/ndvi")
async def get_ndvi(
    lat: Annotated[
        float | None,
        Query(ge=-90, le=90, description="Center latitude (use with lon and size_km)"),
    ] = None,
    lon: Annotated[
        float | None,
        Query(
            ge=-180, le=180, description="Center longitude (use with lat and size_km)"
        ),
    ] = None,
    size_km: Annotated[
        float, Query(ge=1, le=100, description="Box size in km (use with lat/lon)")
    ] = 5.0,
    min_lat: Annotated[
        float | None,
        Query(ge=-90, le=90, description="South bound (use for bbox mode)"),
    ] = None,
    max_lat: Annotated[
        float | None,
        Query(ge=-90, le=90, description="North bound (use for bbox mode)"),
    ] = None,
    min_lon: Annotated[
        float | None,
        Query(ge=-180, le=180, description="West bound (use for bbox mode)"),
    ] = None,
    max_lon: Annotated[
        float | None,
        Query(ge=-180, le=180, description="East bound (use for bbox mode)"),
    ] = None,
    start_date: Annotated[
        str | None,
        Query(description="Start date YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    end_date: Annotated[
        str | None,
        Query(description="End date YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    format: Annotated[
        str, Query(description="Response format: 'stats' or 'raster'")
    ] = "stats",
    _user: dict = require_auth,
):
    """
    Get NDVI (Normalized Difference Vegetation Index) for a location.

    NDVI measures vegetation density and health.
    Range: -1 to +1 (higher = denser/healthier vegetation)

    Interpretation:
    - < 0.1: Bare ground, rock, sand, snow
    - 0.1-0.2: Sparse vegetation
    - 0.2-0.4: Moderate vegetation (shrubs, grassland)
    - 0.4-0.6: Healthy vegetation (crops, forests)
    - > 0.6: Dense vegetation (rainforest, peak growth)

    Data source: Copernicus Sentinel-2

    Usage:
    - Provide lat/lon/size_km for center-based queries
    - OR provide min_lat/max_lat/min_lon/max_lon for bbox queries
    - Set format='raster' for GeoTIFF base64 response
    """
    # Validate format parameter
    if format not in ["stats", "raster"]:
        raise HTTPException(
            status_code=400, detail="Invalid format. Must be 'stats' or 'raster'"
        )

    # Validate bbox coordinates if provided
    if min_lat is not None and max_lat is not None and min_lat >= max_lat:
        raise HTTPException(status_code=400, detail="min_lat must be less than max_lat")

    if min_lon is not None and max_lon is not None and min_lon >= max_lon:
        raise HTTPException(status_code=400, detail="min_lon must be less than max_lon")

    # Validate date format if provided (FastAPI pattern validation catches most cases)
    if start_date and not isinstance(start_date, str):
        raise HTTPException(
            status_code=400, detail="start_date must be a string in YYYY-MM-DD format"
        )

    if end_date and not isinstance(end_date, str):
        raise HTTPException(
            status_code=400, detail="end_date must be a string in YYYY-MM-DD format"
        )

    try:
        result = await copernicus_service.get_ndvi(
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

        if result["status"] == "error":
            raise HTTPException(status_code=400, detail=result["message"])

        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Copernicus API error: {str(e)}")


@router.get("/ndmi")
async def get_ndmi(
    lat: Annotated[
        float | None,
        Query(ge=-90, le=90, description="Center latitude (use with lon and size_km)"),
    ] = None,
    lon: Annotated[
        float | None,
        Query(
            ge=-180, le=180, description="Center longitude (use with lat and size_km)"
        ),
    ] = None,
    size_km: Annotated[
        float, Query(ge=1, le=100, description="Box size in km (use with lat/lon)")
    ] = 5.0,
    min_lat: Annotated[
        float | None,
        Query(ge=-90, le=90, description="South bound (use for bbox mode)"),
    ] = None,
    max_lat: Annotated[
        float | None,
        Query(ge=-90, le=90, description="North bound (use for bbox mode)"),
    ] = None,
    min_lon: Annotated[
        float | None,
        Query(ge=-180, le=180, description="West bound (use for bbox mode)"),
    ] = None,
    max_lon: Annotated[
        float | None,
        Query(ge=-180, le=180, description="East bound (use for bbox mode)"),
    ] = None,
    start_date: Annotated[
        str | None,
        Query(description="Start date YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    end_date: Annotated[
        str | None,
        Query(description="End date YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    format: Annotated[
        str, Query(description="Response format: 'stats' or 'raster'")
    ] = "stats",
    _user: dict = require_auth,
):
    """
    Get NDMI (Normalized Difference Moisture Index) for a location.

    NDMI measures vegetation water content - critical for fire risk assessment.
    Range: -1 to +1 (higher = more moisture)

    Fire Risk Correlation:
    - NDMI < -0.1: HIGH fire risk (dry fuels)
    - NDMI -0.1 to 0.1: MODERATE fire risk
    - NDMI > 0.1: LOW fire risk (moist fuels)

    Data source: Copernicus Sentinel-2

    Usage:
    - Provide lat/lon/size_km for center-based queries
    - OR provide min_lat/max_lat/min_lon/max_lon for bbox queries
    - Set format='raster' for GeoTIFF base64 response
    """
    # Validate format parameter
    if format not in ["stats", "raster"]:
        raise HTTPException(
            status_code=400, detail="Invalid format. Must be 'stats' or 'raster'"
        )

    # Validate bbox coordinates if provided
    if min_lat is not None and max_lat is not None and min_lat >= max_lat:
        raise HTTPException(status_code=400, detail="min_lat must be less than max_lat")

    if min_lon is not None and max_lon is not None and min_lon >= max_lon:
        raise HTTPException(status_code=400, detail="min_lon must be less than max_lon")

    # Validate date format if provided (FastAPI pattern validation catches most cases)
    if start_date and not isinstance(start_date, str):
        raise HTTPException(
            status_code=400, detail="start_date must be a string in YYYY-MM-DD format"
        )

    if end_date and not isinstance(end_date, str):
        raise HTTPException(
            status_code=400, detail="end_date must be a string in YYYY-MM-DD format"
        )

    try:
        result = await copernicus_service.get_ndmi(
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

        if result["status"] == "error":
            raise HTTPException(status_code=400, detail=result["message"])

        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Copernicus API error: {str(e)}")
