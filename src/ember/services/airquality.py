"""EPA AirNow air quality service."""

from datetime import datetime, timezone
from time import time
from typing import Any

import httpx

from ember.config import settings
from ember.logging import get_logger

logger = get_logger(__name__)

AIRNOW_BASE_URL = "https://www.airnowapi.org/aq/observation/latLong/current/"
USER_AGENT = "Stellaris/Ember Fire Intelligence Platform (contact@stellaris.app)"

# AQI category definitions per EPA standard
AQI_CATEGORIES = {
    (0, 50): "Good",
    (51, 100): "Moderate",
    (101, 150): "Unhealthy for Sensitive Groups",
    (151, 200): "Unhealthy",
    (201, 300): "Very Unhealthy",
    (301, 500): "Hazardous",
}

# Cache: 1 hour
_aqi_cache: dict[str, dict] = {}
_AQI_CACHE_TTL = 3600  # 1 hour
_AQI_CACHE_MAX_SIZE = 500


def _get_aqi_category(aqi: int) -> str:
    """Get EPA category name for an AQI value."""
    for (low, high), category in AQI_CATEGORIES.items():
        if low <= aqi <= high:
            return category
    if aqi > 500:
        return "Hazardous"
    return "Unknown"


class AirQualityService:
    """EPA AirNow API client for air quality data."""

    def __init__(self):
        self.timeout = settings.http_timeout

    async def get_air_quality(
        self,
        lat: float,
        lon: float,
        distance_miles: float = 25.0,
    ) -> dict[str, Any]:
        """Get current AQI and pollutant levels.

        Args:
            lat: Latitude
            lon: Longitude
            distance_miles: Search radius for monitoring stations (1-100)

        Returns:
            Dict with AQI, category, dominant pollutant, and individual pollutant levels.
        """
        api_key = settings.airnow_api_key
        if not api_key:
            raise ValueError(
                "AIRNOW_API_KEY not configured. "
                "Get a free key at https://docs.airnowapi.org/account/request/"
            )

        distance_miles = min(max(distance_miles, 1.0), 100.0)

        # Check cache
        cache_key = f"airquality:{lat:.3f}:{lon:.3f}"
        cached = _aqi_cache.get(cache_key)
        if cached and (time() - cached["timestamp"] < _AQI_CACHE_TTL):
            return cached["data"]

        async with httpx.AsyncClient() as client:
            response = await client.get(
                AIRNOW_BASE_URL,
                params={
                    "format": "application/json",
                    "latitude": lat,
                    "longitude": lon,
                    "distance": distance_miles,
                    "API_KEY": api_key,
                },
                headers={"User-Agent": USER_AGENT},
                timeout=self.timeout,
            )
            response.raise_for_status()

        observations = response.json()

        if not observations:
            return {
                "status": "no_data",
                "message": f"No monitoring stations found within {distance_miles} miles",
                "latitude": lat,
                "longitude": lon,
                "source": "EPA AirNow",
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            }

        # Parse pollutant observations
        pollutants = {}
        max_aqi = 0
        dominant_pollutant = None

        for obs in observations:
            name = obs.get("ParameterName", "Unknown")
            aqi_value = obs.get("AQI", 0)

            # Map pollutant names to standard keys
            key = self._pollutant_key(name)
            pollutants[key] = {
                "aqi": aqi_value,
                "concentration": obs.get("Concentration"),
                "unit": obs.get("Unit", "µg/m³"),
            }

            if aqi_value > max_aqi:
                max_aqi = aqi_value
                dominant_pollutant = name

        data = {
            "status": "success",
            "latitude": lat,
            "longitude": lon,
            "aqi": max_aqi,
            "category": _get_aqi_category(max_aqi),
            "dominant_pollutant": dominant_pollutant,
            "pollutants": pollutants,
            "source": "EPA AirNow",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }

        # Store in cache
        if len(_aqi_cache) >= _AQI_CACHE_MAX_SIZE:
            _aqi_cache.clear()
        _aqi_cache[cache_key] = {"timestamp": time(), "data": data}

        return data

    @staticmethod
    def _pollutant_key(name: str) -> str:
        """Map EPA pollutant names to short keys."""
        mapping = {
            "PM2.5": "pm25",
            "PM10": "pm10",
            "O3": "ozone",
            "NO2": "no2",
            "SO2": "so2",
            "CO": "co",
        }
        return mapping.get(name, name.lower().replace(".", ""))


airquality_service = AirQualityService()
