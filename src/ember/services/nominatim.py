"""Nominatim geocoding service (OpenStreetMap)."""

from time import time
from typing import Any

import httpx

from ember.config import settings

NOMINATIM_BASE_URL = "https://nominatim.openstreetmap.org"
USER_AGENT = "Ember/1.0 (Wildfire Intelligence Platform)"

# Cache for geocoding (addresses rarely change)
_geocode_cache: dict[str, dict] = {}
_GEOCODE_CACHE_TTL = 86400  # 24 hours
_GEOCODE_CACHE_MAX_SIZE = 1000


class NominatimService:
    """Service for geocoding via OpenStreetMap Nominatim."""

    def __init__(self):
        self.timeout = settings.http_timeout
        self.headers = {"User-Agent": USER_AGENT}

    async def geocode(self, address: str, country: str | None = None) -> dict[str, Any]:
        """
        Convert address to coordinates.

        Args:
            address: Address string to geocode
            country: Optional ISO country code to filter results

        Returns:
            Dict with coordinates and address details
        """
        # Check cache
        cache_key = f"geocode:{address.lower()}:{country or ''}"
        cached = _geocode_cache.get(cache_key)
        if cached and (time() - cached["timestamp"] < _GEOCODE_CACHE_TTL):
            return cached["data"]

        params = {
            "q": address,
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": 1,
        }
        if country:
            params["countrycodes"] = country

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{NOMINATIM_BASE_URL}/search",
                params=params,
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()

        results = response.json()
        if not results:
            return {"status": "not_found", "address": address}

        result = results[0]
        data = {
            "status": "success",
            "latitude": float(result["lat"]),
            "longitude": float(result["lon"]),
            "display_name": result.get("display_name", ""),
            "place_id": result.get("place_id"),
            "osm_type": result.get("osm_type"),
            "osm_id": result.get("osm_id"),
            "address_components": self._extract_address(result.get("address", {})),
            "bounding_box": self._extract_bbox(result.get("boundingbox")),
        }

        # Store in cache
        if len(_geocode_cache) >= _GEOCODE_CACHE_MAX_SIZE:
            _geocode_cache.clear()
        _geocode_cache[cache_key] = {"timestamp": time(), "data": data}

        return data

    async def reverse_geocode(
        self, lat: float, lon: float, zoom: int = 18
    ) -> dict[str, Any]:
        """
        Convert coordinates to address.

        Args:
            lat: Latitude
            lon: Longitude
            zoom: Detail level (0-18, higher = more specific)

        Returns:
            Dict with address details
        """
        # Check cache (round to 4 decimals = ~11m precision)
        cache_key = f"reverse:{lat:.4f},{lon:.4f}:{zoom}"
        cached = _geocode_cache.get(cache_key)
        if cached and (time() - cached["timestamp"] < _GEOCODE_CACHE_TTL):
            return cached["data"]

        params = {
            "lat": lat,
            "lon": lon,
            "format": "jsonv2",
            "addressdetails": 1,
            "zoom": zoom,
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{NOMINATIM_BASE_URL}/reverse",
                params=params,
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()

        result = response.json()
        if "error" in result:
            return {"status": "not_found", "latitude": lat, "longitude": lon}

        data = {
            "status": "success",
            "latitude": lat,
            "longitude": lon,
            "display_name": result.get("display_name", ""),
            "place_id": result.get("place_id"),
            "osm_type": result.get("osm_type"),
            "osm_id": result.get("osm_id"),
            "address_components": self._extract_address(result.get("address", {})),
        }

        # Store in cache
        if len(_geocode_cache) >= _GEOCODE_CACHE_MAX_SIZE:
            _geocode_cache.clear()
        _geocode_cache[cache_key] = {"timestamp": time(), "data": data}

        return data

    def _extract_address(self, address: dict) -> dict:
        """Extract standardized address components."""
        return {
            "house_number": address.get("house_number"),
            "road": address.get("road"),
            "suburb": address.get("suburb") or address.get("neighbourhood"),
            "city": (
                address.get("city")
                or address.get("town")
                or address.get("village")
                or address.get("municipality")
            ),
            "county": address.get("county"),
            "state": address.get("state"),
            "country": address.get("country"),
            "postcode": address.get("postcode"),
        }

    def _extract_bbox(self, bbox: list | None) -> dict | None:
        """Extract bounding box from Nominatim format."""
        if not bbox or len(bbox) != 4:
            return None
        return {
            "south": float(bbox[0]),
            "north": float(bbox[1]),
            "west": float(bbox[2]),
            "east": float(bbox[3]),
        }


nominatim_service = NominatimService()
