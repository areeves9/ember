"""NWS fire weather alert endpoints."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from ember.auth import require_auth
from ember.services.nws import nws_service

router = APIRouter(prefix="/nws", tags=["nws"])


@router.get("/fire-weather-alerts")
async def get_fire_weather_alerts(
    lat: Annotated[float | None, Query(ge=-90, le=90, description="Point query latitude")] = None,
    lon: Annotated[
        float | None, Query(ge=-180, le=180, description="Point query longitude")
    ] = None,
    state: Annotated[
        str | None,
        Query(min_length=2, max_length=2, description="2-letter state code"),
    ] = None,
    _user: dict = require_auth,
):
    """Get active Red Flag Warnings and Fire Weather Watches.

    Supports two query modes:
    - **Point**: provide `lat` and `lon` for alerts at a specific location
    - **State**: provide 2-letter `state` code for all fire weather alerts in a state

    Returns GeoJSON alert polygons with metadata, filtered to fire-relevant events only.
    CONUS coverage only (NWS API covers US territories).
    """
    if lat is None and lon is None and state is None:
        raise HTTPException(status_code=400, detail="Must provide lat/lon or state parameter")
    if (lat is None) != (lon is None):
        raise HTTPException(status_code=400, detail="Both lat and lon are required for point query")

    try:
        result = await nws_service.get_fire_weather_alerts(lat=lat, lon=lon, state=state)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"NWS API error: {str(e)}")
