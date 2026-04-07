"""Satellite imagery endpoints - Copernicus Sentinel-2."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from ember.auth import require_auth
from ember.services.copernicus import copernicus_service

router = APIRouter(prefix="/imagery", tags=["imagery"])


@router.get("/truecolor")
async def get_truecolor(
    lat: Annotated[
        float | None,
        Query(ge=-90, le=90, description="Center latitude (use with lon and size_km)"),
    ] = None,
    lon: Annotated[
        float | None,
        Query(ge=-180, le=180, description="Center longitude (use with lat and size_km)"),
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
        str, Query(description="Response format: 'png' (default) or 'raster' (GeoTIFF)")
    ] = "png",
    max_size: Annotated[
        int | None,
        Query(
            ge=64,
            le=2048,
            description="Override output resolution in pixels (default: auto by area)",
        ),
    ] = None,
    _user: dict = require_auth,
):
    """
    Get true-color RGB satellite imagery for a location.

    Returns a natural-color photograph from Sentinel-2 combining Red (B04),
    Green (B03), and Blue (B02) bands with 2.5x gain compensation.

    Uses high resolution tier (256/512/1024 pixels based on area).

    Usage:
    - Provide lat/lon/size_km for center-based queries
    - OR provide min_lat/max_lat/min_lon/max_lon for bbox queries
    - Default format is PNG (pre-rendered image, no GeoTIFF parsing needed)

    Data source: Copernicus Sentinel-2 L2A
    """
    if format not in ("png", "raster"):
        raise HTTPException(
            status_code=400,
            detail="Invalid format. Must be 'png' or 'raster'",
        )

    if min_lat is not None and max_lat is not None and min_lat >= max_lat:
        raise HTTPException(status_code=400, detail="min_lat must be less than max_lat")

    if min_lon is not None and max_lon is not None and min_lon >= max_lon:
        raise HTTPException(status_code=400, detail="min_lon must be less than max_lon")

    try:
        result = await copernicus_service.get_truecolor(
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
            max_size=max_size,
        )

        if result["status"] == "error":
            raise HTTPException(status_code=400, detail=result["message"])

        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Copernicus API error: {str(e)}")
