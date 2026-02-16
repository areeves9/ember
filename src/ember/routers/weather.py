"""Weather endpoints - Open-Meteo."""

from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Query

from ember.auth import require_auth
from ember.services.openmeteo import openmeteo_service

router = APIRouter(prefix="/weather", tags=["weather"])


@router.get("/current")
async def get_current_weather(
    lat: Annotated[float, Query(ge=-90, le=90, description="Latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Longitude")],
    variables: Annotated[
        Optional[str],
        Query(
            description="Comma-separated list of Open-Meteo variable names (e.g., 'soil_moisture_0_to_1cm,temperature_2m'). If not provided, returns default variables."
        ),
    ] = None,
    _user: dict = require_auth,
):
    """
    Get current weather conditions at a location.

    Returns temperature, humidity, wind speed/direction, and conditions.
    Optionally specify custom variables to retrieve from Open-Meteo.
    """
    try:
        result = await openmeteo_service.get_current_weather(lat, lon, variables)
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Weather API error: {str(e)}")


@router.get("/forecast")
async def get_forecast(
    lat: Annotated[float, Query(ge=-90, le=90, description="Latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Longitude")],
    days: Annotated[int, Query(ge=1, le=7, description="Forecast days")] = 3,
    _user: dict = require_auth,
):
    """
    Get weather forecast for a location.

    Returns daily forecast with temperature, precipitation, and wind.
    """
    try:
        result = await openmeteo_service.get_forecast(lat, lon, days)
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Weather API error: {str(e)}")
