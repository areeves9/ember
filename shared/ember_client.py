"""
Ember API client for Nova frontend.

Provides typed Python wrappers around Ember's REST API endpoints.
Handles M2M authentication, error propagation, and parameter validation.
"""

import httpx
from typing import Any, Optional


class EmberClientError(Exception):
    """Base exception for Ember client errors."""
    pass


class EmberAPIError(EmberClientError):
    """Error from Ember API (4xx/5xx responses)."""
    pass


class EmberNetworkError(EmberClientError):
    """Network/connection error when calling Ember."""
    pass


# Global client configuration
EMBER_BASE_URL = "http://localhost:8001/api/v1"
M2M_TOKEN = None  # Set via configure_client()


async def _request(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    """
    Internal helper for making authenticated requests to Ember.
    
    Args:
        endpoint: API endpoint path (e.g., "fires", "weather/current")
        params: Query parameters
        
    Returns:
        dict: Parsed JSON response
        
    Raises:
        httpx.HTTPStatusError: If Ember returns error status
        httpx.RequestError: If cannot reach Ember
    """
    url = f"{EMBER_BASE_URL}/{endpoint}"
    
    headers = {}
    if M2M_TOKEN:
        headers["Authorization"] = f"Bearer {M2M_TOKEN}"
    
    async with httpx.AsyncClient() as client:
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()


def configure_client(base_url: str = None, m2m_token: str = None) -> None:
    """
    Configure Ember client with base URL and M2M token.
    
    Args:
        base_url: Base URL of Ember API (default: http://localhost:8001/api/v1)
        m2m_token: M2M token for authentication
    """
    global EMBER_BASE_URL, M2M_TOKEN
    
    if base_url:
        EMBER_BASE_URL = base_url.rstrip("/")
    
    if m2m_token:
        M2M_TOKEN = m2m_token


# Vegetation endpoints

async def get_ndvi(
    lat: float,
    lon: float,
    size_km: float = 5.0,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """
    Get NDVI (Normalized Difference Vegetation Index) for a location.
    
    Args:
        lat: Latitude in decimal degrees (-90 to 90)
        lon: Longitude in decimal degrees (-180 to 180)
        size_km: Box size in kilometers (default 5.0)
        start_date: Start date YYYY-MM-DD
        end_date: End date YYYY-MM-DD
        
    Returns:
        dict: NDVI response with vegetation index values
        
    Raises:
        httpx.HTTPStatusError: If Ember returns error status
        httpx.RequestError: If cannot reach Ember
    """
    params = {
        "lat": lat,
        "lon": lon,
        "size_km": size_km,
    }
    
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    
    return await _request("vegetation/ndvi", params)


async def get_ndmi(
    lat: float,
    lon: float,
    size_km: float = 5.0,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """
    Get NDMI (Normalized Difference Moisture Index) for a location.
    
    Args:
        lat: Latitude in decimal degrees (-90 to 90)
        lon: Longitude in decimal degrees (-180 to 180)
        size_km: Box size in kilometers (default 5.0)
        start_date: Start date YYYY-MM-DD
        end_date: End date YYYY-MM-DD
        
    Returns:
        dict: NDMI response with moisture index values
        
    Raises:
        httpx.HTTPStatusError: If Ember returns error status
        httpx.RequestError: If cannot reach Ember
    """
    params = {
        "lat": lat,
        "lon": lon,
        "size_km": size_km,
    }
    
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    
    return await _request("vegetation/ndmi", params)


# Fire detection endpoints

async def get_active_fires(
    bbox: Optional[list[float]] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_km: Optional[float] = None,
    confidence: Optional[str] = None,
    hours: int = 24,
) -> dict[str, Any]:
    """
    Get active fire detections from Ember.

    Supports two query modes:
    1. Bounding box: Provide bbox parameter
    2. Radius search: Provide lat, lon, and radius_km

    Args:
        bbox: Bounding box [min_lon, min_lat, max_lon, max_lat]
        lat: Latitude in decimal degrees (-90 to 90)
        lon: Longitude in decimal degrees (-180 to 180)
        radius_km: Search radius in kilometers
        confidence: Fire confidence filter ("low", "nominal", "high")
        hours: Hours to look back (1-168, default 24)

    Returns:
        dict: Active fire response with detection points

    Raises:
        httpx.HTTPStatusError: If Ember returns error status
        httpx.RequestError: If cannot reach Ember
    """
    params = {"hours": hours}

    if bbox:
        params["bbox"] = ",".join(map(str, bbox))

    if lat is not None:
        params["lat"] = lat
    if lon is not None:
        params["lon"] = lon
    if radius_km is not None:
        params["radius_km"] = radius_km

    if confidence:
        params["confidence"] = confidence

    return await _request("fires", params)


# Weather endpoints

async def get_weather_forecast(
    lat: float,
    lon: float,
    days: int = 7,
    hourly: bool = False,
    include_alerts: bool = True,
) -> dict[str, Any]:
    """
    Get weather forecast from Ember.

    Args:
        lat: Latitude in decimal degrees (-90 to 90)
        lon: Longitude in decimal degrees (-180 to 180)
        days: Forecast duration in days (1-16, default 7)
        hourly: Include hourly breakdown (default False)
        include_alerts: Include weather alerts (default True)

    Returns:
        dict: Weather forecast response with conditions and alerts

    Raises:
        httpx.HTTPStatusError: If Ember returns error status
        httpx.RequestError: If cannot reach Ember
    """
    params = {
        "lat": lat,
        "lon": lon,
        "days": days,
        "hourly": hourly,
        "include_alerts": include_alerts,
    }

    return await _request("weather/forecast", params)


async def get_current_weather(
    lat: float,
    lon: float,
) -> dict[str, Any]:
    """
    Get current weather conditions from Ember.

    Args:
        lat: Latitude in decimal degrees (-90 to 90)
        lon: Longitude in decimal degrees (-180 to 180)

    Returns:
        dict: Current weather response with real-time conditions

    Raises:
        httpx.HTTPStatusError: If Ember returns error status
        httpx.RequestError: If cannot reach Ember
    """
    params = {
        "lat": lat,
        "lon": lon,
    }

    return await _request("weather/current", params)