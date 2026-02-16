"""Weather endpoints - Open-Meteo."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from ember.auth import require_auth
from ember.services.openmeteo import openmeteo_service

router = APIRouter(prefix="/weather", tags=["weather"])


@router.get("/current")
async def get_current_weather(
    lat: Annotated[float, Query(ge=-90, le=90, description="Latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Longitude")],
    variables: Annotated[
        str | None,
        Query(
            description="Comma-separated list of Open-Meteo variable names (e.g., 'soil_moisture_0_to_1cm,temperature_2m'). If not provided, returns default variables."
        ),
    ] = None,
    _user: dict = require_auth,
):
    """
    Get current weather conditions at a location.

    Response format depends on whether custom variables are requested:

    - **Without variables** (default): Returns transformed format with keys like
      `temperature_c`, `humidity_pct`, `wind_speed_kmh` for backward compatibility.

    - **With variables**: Returns raw Open-Meteo format with requested variable names
      as keys (e.g., `soil_moisture_0_to_1cm`, `temperature_2m`). The response includes
      `current` and `current_units` for proper interpretation.

    See Open-Meteo documentation for available current weather variables:
    https://open-meteo.com/en/docs
    """
    try:
        result = await openmeteo_service.get_current_weather(lat, lon, variables=variables)
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Weather API error: {str(e)}")


@router.get("/historical")
async def get_historical_weather(
    lat: Annotated[float, Query(ge=-90, le=90, description="Latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Longitude")],
    start_date: Annotated[
        str,
        Query(
            pattern=r"^\d{4}-\d{2}-\d{2}$",
            description="Start date (YYYY-MM-DD)",
        ),
    ],
    end_date: Annotated[
        str,
        Query(
            pattern=r"^\d{4}-\d{2}-\d{2}$",
            description="End date (YYYY-MM-DD)",
        ),
    ],
    _user: dict = require_auth,
):
    """
    Get historical daily weather data for a date range.

    Returns daily weather records from 1940 to present.
    Data includes temperature, humidity, precipitation, and wind.
    """
    try:
        result = await openmeteo_service.get_historical_weather(
            lat, lon, start_date, end_date
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Weather API error: {str(e)}")


@router.get("/historical/hourly")
async def get_hourly_historical_weather(
    lat: Annotated[float, Query(ge=-90, le=90, description="Latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Longitude")],
    start_date: Annotated[
        str,
        Query(
            pattern=r"^\d{4}-\d{2}-\d{2}$",
            description="Start date (YYYY-MM-DD)",
        ),
    ],
    end_date: Annotated[
        str,
        Query(
            pattern=r"^\d{4}-\d{2}-\d{2}$",
            description="End date (YYYY-MM-DD)",
        ),
    ],
    _user: dict = require_auth,
):
    """
    Get historical hourly weather data for a date range.

    Returns hourly weather records from 1940 to present.
    Data includes temperature, humidity, precipitation, and wind for each hour.
    """
    try:
        result = await openmeteo_service.get_hourly_historical_weather(
            lat, lon, start_date, end_date
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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


@router.get("/forecast/hourly")
async def get_hourly_forecast(
    lat: Annotated[float, Query(ge=-90, le=90, description="Latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Longitude")],
    hours: Annotated[
        int,
        Query(ge=1, le=384, description="Forecast hours (max 384 = 16 days)")
    ] = 24,
    _user: dict = require_auth,
):
    """
    Get hourly weather forecast for a location.

    Returns hourly forecast with temperature, precipitation, wind, and humidity.
    Maximum forecast range is 384 hours (16 days).
    """
    try:
        result = await openmeteo_service.get_hourly_forecast(lat, lon, hours)
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Weather API error: {str(e)}")
