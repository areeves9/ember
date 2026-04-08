"""Fire data endpoints - NASA FIRMS."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from ember.auth import require_auth
from ember.services.firms import SATELLITE_SOURCES, firms_service

router = APIRouter(prefix="/fires", tags=["fires"])


@router.get("")
async def get_fires(
    min_lat: Annotated[float, Query(ge=-90, le=90, description="Southern boundary")],
    max_lat: Annotated[float, Query(ge=-90, le=90, description="Northern boundary")],
    min_lon: Annotated[float, Query(ge=-180, le=180, description="Western boundary")],
    max_lon: Annotated[float, Query(ge=-180, le=180, description="Eastern boundary")],
    source: Annotated[str, Query(description="Satellite source")] = "VIIRS_SNPP_NRT",
    days_back: Annotated[int, Query(ge=1, le=10, description="Days of historical data")] = 2,
    _user: dict = require_auth,
):
    """
    Get active fire detections for a bounding box.

    Returns fire detections from NASA FIRMS with GeoJSON for map rendering.
    """
    if source not in SATELLITE_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid source. Must be one of: {SATELLITE_SOURCES}",
        )

    try:
        result = await firms_service.get_fires(
            min_lat=min_lat,
            max_lat=max_lat,
            min_lon=min_lon,
            max_lon=max_lon,
            source=source,
            days_back=days_back,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"FIRMS API error: {str(e)}")


@router.get("/timeline")
async def get_fire_timeline(
    lat: Annotated[float, Query(ge=-90, le=90, description="Center latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Center longitude")],
    radius_km: Annotated[float, Query(ge=1, le=50, description="Search radius in km")] = 5.0,
    hours: Annotated[int, Query(ge=1, le=120, description="Lookback window in hours")] = 72,
    source: Annotated[str, Query(description="Satellite source")] = "VIIRS_SNPP_NRT",
    _user: dict = require_auth,
):
    """Get thermal timeline for a location — FRP evolution over time.

    Returns observations grouped by satellite pass with trend analysis.
    Useful for answering: When did this start? Is it growing or declining?
    """
    if source not in SATELLITE_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid source. Must be one of: {SATELLITE_SOURCES}",
        )

    try:
        result = await firms_service.get_timeline(
            lat=lat,
            lon=lon,
            radius_km=radius_km,
            hours=hours,
            source=source,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"FIRMS API error: {str(e)}")


@router.get("/sources")
async def list_sources():
    """List available satellite data sources."""
    return {
        "sources": [
            {"id": "VIIRS_SNPP_NRT", "name": "VIIRS SNPP", "resolution": "375m"},
            {"id": "VIIRS_NOAA20_NRT", "name": "VIIRS NOAA-20", "resolution": "375m"},
            {"id": "VIIRS_NOAA21_NRT", "name": "VIIRS NOAA-21", "resolution": "375m"},
            {"id": "MODIS_NRT", "name": "MODIS", "resolution": "1km"},
            {"id": "GOES16_NRT", "name": "GOES-16 East", "resolution": "2km"},
            {"id": "GOES17_NRT", "name": "GOES-17 West", "resolution": "2km"},
            {"id": "GOES18_NRT", "name": "GOES-18 West", "resolution": "2km"},
        ]
    }
