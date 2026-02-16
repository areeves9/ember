"""Open-Meteo weather service (free, no API key required)."""

from time import time
from typing import Any

import httpx

from ember.config import settings

OPENMETEO_BASE_URL = "https://api.open-meteo.com/v1/forecast"

# Cache for weather queries (weather changes, but not per-second)
_weather_cache: dict[str, dict] = {}
_WEATHER_CACHE_TTL = 300  # 5 minutes
_WEATHER_CACHE_MAX_SIZE = 500


class OpenMeteoService:
    """Service for fetching weather data from Open-Meteo."""

    def __init__(self):
        self.timeout = settings.http_timeout

    async def get_current_weather(
        self, lat: float, lon: float, variables: str | None = None
    ) -> dict[str, Any]:
        """
        Get current weather conditions at a location.

        Args:
            lat: Latitude
            lon: Longitude
            variables: Optional comma-separated list of Open-Meteo variable names.
                      If None, returns default variables with transformed response format.
                      If provided, returns raw Open-Meteo response format.

        Returns:
            Dict with current weather conditions
        """
        # Parse and normalize variables for cache key and API request
        if variables:
            # Split, strip whitespace, filter empty strings
            current_vars = [v.strip() for v in variables.split(",") if v.strip()]
            # De-duplicate while preserving order to avoid cache bloat and redundant API requests
            seen = set()
            current_vars = [v for v in current_vars if not (v in seen or seen.add(v))]
            # If filtering/de-duplication resulted in empty list, fall back to defaults
            if not current_vars:
                variables = None

        # Use default variables if none provided or parsing resulted in empty list
        if not variables:
            current_vars = [
                "temperature_2m",
                "relative_humidity_2m",
                "apparent_temperature",
                "precipitation",
                "weather_code",
                "wind_speed_10m",
                "wind_direction_10m",
                "wind_gusts_10m",
            ]
            vars_key = "default"
        else:
            # Sort for cache normalization (temp,humidity = humidity,temp)
            vars_key = ",".join(sorted(current_vars))

        # Check cache (round to 2 decimals = ~1km precision)
        cache_key = f"weather:current:{lat:.2f},{lon:.2f}:{vars_key}"
        cached = _weather_cache.get(cache_key)
        if cached and (time() - cached["timestamp"] < _WEATHER_CACHE_TTL):
            return cached["data"]

        params = {
            "latitude": lat,
            "longitude": lon,
            "current": current_vars,
            "timezone": "auto",
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(
                OPENMETEO_BASE_URL,
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()

        data = response.json()
        current = data.get("current", {})

        # If custom variables requested, return raw Open-Meteo format
        if variables:
            result = {
                "status": "success",
                "latitude": lat,
                "longitude": lon,
                "timezone": data.get("timezone"),
                "current": current,
                "current_units": data.get("current_units", {}),
            }
        else:
            # Default variables: return transformed format for backward compatibility
            result = {
                "status": "success",
                "latitude": lat,
                "longitude": lon,
                "timezone": data.get("timezone"),
                "current": {
                    "temperature_c": current.get("temperature_2m"),
                    "feels_like_c": current.get("apparent_temperature"),
                    "humidity_pct": current.get("relative_humidity_2m"),
                    "precipitation_mm": current.get("precipitation"),
                    "weather_code": current.get("weather_code"),
                    "wind_speed_kmh": current.get("wind_speed_10m"),
                    "wind_direction_deg": current.get("wind_direction_10m"),
                    "wind_gusts_kmh": current.get("wind_gusts_10m"),
                    "conditions": self._weather_code_to_text(current.get("weather_code")),
                },
            }

        # Store in cache
        if len(_weather_cache) >= _WEATHER_CACHE_MAX_SIZE:
            _weather_cache.clear()
        _weather_cache[cache_key] = {"timestamp": time(), "data": result}

        return result

    async def get_forecast(
        self, lat: float, lon: float, days: int = 3
    ) -> dict[str, Any]:
        """
        Get weather forecast for a location.

        Args:
            lat: Latitude
            lon: Longitude
            days: Number of forecast days (1-7)

        Returns:
            Dict with daily forecast
        """
        days = max(1, min(7, days))

        # Check cache
        cache_key = f"weather:forecast:{lat:.2f},{lon:.2f}:{days}"
        cached = _weather_cache.get(cache_key)
        if cached and (time() - cached["timestamp"] < _WEATHER_CACHE_TTL):
            return cached["data"]

        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": [
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_probability_max",
                "wind_speed_10m_max",
                "wind_direction_10m_dominant",
                "weather_code",
            ],
            "forecast_days": days,
            "timezone": "auto",
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(
                OPENMETEO_BASE_URL,
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()

        data = response.json()
        daily = data.get("daily", {})

        # Build daily forecast list
        forecast = []
        dates = daily.get("time", [])
        for i, date in enumerate(dates):
            forecast.append(
                {
                    "date": date,
                    "temp_max_c": daily.get("temperature_2m_max", [None])[i],
                    "temp_min_c": daily.get("temperature_2m_min", [None])[i],
                    "precip_probability_pct": daily.get(
                        "precipitation_probability_max", [None]
                    )[i],
                    "wind_speed_max_kmh": daily.get("wind_speed_10m_max", [None])[i],
                    "wind_direction_deg": daily.get(
                        "wind_direction_10m_dominant", [None]
                    )[i],
                    "weather_code": daily.get("weather_code", [None])[i],
                    "conditions": self._weather_code_to_text(
                        daily.get("weather_code", [None])[i]
                    ),
                }
            )

        result = {
            "status": "success",
            "latitude": lat,
            "longitude": lon,
            "timezone": data.get("timezone"),
            "forecast": forecast,
        }

        # Store in cache
        if len(_weather_cache) >= _WEATHER_CACHE_MAX_SIZE:
            _weather_cache.clear()
        _weather_cache[cache_key] = {"timestamp": time(), "data": result}

        return result

    async def get_hourly_forecast(
        self, lat: float, lon: float, hours: int = 24
    ) -> dict[str, Any]:
        """
        Get hourly weather forecast for a location.

        Args:
            lat: Latitude
            lon: Longitude
            hours: Number of forecast hours (1-384, up to 16 days)

        Returns:
            Dict with hourly forecast
        """
        # Clamp to valid range (Open-Meteo supports up to 16 days = 384 hours)
        hours = max(1, min(384, hours))

        # Convert hours to days for forecast_days parameter (ceiling division)
        forecast_days = (hours + 23) // 24

        # Check cache (1 hour TTL - hourly data updates frequently)
        cache_key = f"weather:hourly_forecast:{lat:.2f},{lon:.2f}:{hours}h"
        cached = _weather_cache.get(cache_key)
        if cached and (time() - cached["timestamp"] < _WEATHER_CACHE_TTL):
            return cached["data"]

        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": [
                "temperature_2m",
                "relative_humidity_2m",
                "precipitation",
                "wind_speed_10m",
                "wind_direction_10m",
                "wind_gusts_10m",
                "apparent_temperature",
            ],
            "forecast_days": forecast_days,
            "timezone": "auto",
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(
                OPENMETEO_BASE_URL,  # Uses /v1/forecast
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()

        data = response.json()
        hourly = data.get("hourly", {})

        # Build hourly forecast list (limit to requested hours)
        timestamps = hourly.get("time", [])[:hours]
        temps = hourly.get("temperature_2m", [])[:hours]
        humidities = hourly.get("relative_humidity_2m", [])[:hours]
        precips = hourly.get("precipitation", [])[:hours]
        wind_speeds = hourly.get("wind_speed_10m", [])[:hours]
        wind_dirs = hourly.get("wind_direction_10m", [])[:hours]
        wind_gusts = hourly.get("wind_gusts_10m", [])[:hours]
        apparent_temps = hourly.get("apparent_temperature", [])[:hours]

        forecast = []
        for i, timestamp in enumerate(timestamps):
            forecast.append({
                "timestamp": timestamp,
                "temperature_c": temps[i] if i < len(temps) else None,
                "humidity_pct": humidities[i] if i < len(humidities) else None,
                "precipitation_mm": precips[i] if i < len(precips) else None,
                "wind_speed_kmh": wind_speeds[i] if i < len(wind_speeds) else None,
                "wind_direction_deg": wind_dirs[i] if i < len(wind_dirs) else None,
                "wind_gusts_kmh": wind_gusts[i] if i < len(wind_gusts) else None,
                "feels_like_c": apparent_temps[i] if i < len(apparent_temps) else None,
            })

        result = {
            "status": "success",
            "latitude": lat,
            "longitude": lon,
            "timezone": data.get("timezone"),
            "hourly_forecast": forecast,
            "forecast_hours": len(forecast),
        }

        # Store in cache (1 hour TTL - hourly data updates frequently)
        if len(_weather_cache) >= _WEATHER_CACHE_MAX_SIZE:
            _weather_cache.clear()
        _weather_cache[cache_key] = {"timestamp": time(), "data": result}

        return result

    def _weather_code_to_text(self, code: int | None) -> str:
        """Convert WMO weather code to human-readable text."""
        if code is None:
            return "Unknown"

        codes = {
            0: "Clear sky",
            1: "Mainly clear",
            2: "Partly cloudy",
            3: "Overcast",
            45: "Foggy",
            48: "Depositing rime fog",
            51: "Light drizzle",
            53: "Moderate drizzle",
            55: "Dense drizzle",
            61: "Slight rain",
            63: "Moderate rain",
            65: "Heavy rain",
            71: "Slight snow",
            73: "Moderate snow",
            75: "Heavy snow",
            77: "Snow grains",
            80: "Slight rain showers",
            81: "Moderate rain showers",
            82: "Violent rain showers",
            85: "Slight snow showers",
            86: "Heavy snow showers",
            95: "Thunderstorm",
            96: "Thunderstorm with slight hail",
            99: "Thunderstorm with heavy hail",
        }
        return codes.get(code, "Unknown")


openmeteo_service = OpenMeteoService()
