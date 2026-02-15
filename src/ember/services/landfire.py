"""LANDFIRE fuel model service.

Supports two backends:
1. COG (preferred) - Direct S3 queries via HTTP range requests
2. REST API (fallback) - LANDFIRE ArcGIS REST service
"""

from typing import Any

import httpx

from ember.config import settings
from ember.logging import get_logger
from ember.services.cog import get_landfire_cog_service

logger = get_logger(__name__)

# LANDFIRE ArcGIS REST service for FBFM40 (fuel models)
LANDFIRE_URL = "https://landfire.cr.usgs.gov/arcgis/rest/services/Landfire/US_200/MapServer/identify"

# FBFM40 fuel model descriptions
FUEL_MODELS = {
    "NB1": ("Non-burnable", "Urban/developed"),
    "NB2": ("Non-burnable", "Snow/ice"),
    "NB3": ("Non-burnable", "Agriculture"),
    "NB8": ("Non-burnable", "Open water"),
    "NB9": ("Non-burnable", "Bare ground"),
    "GR1": ("Grass", "Short, sparse dry climate grass"),
    "GR2": ("Grass", "Low load, dry climate grass"),
    "GR3": ("Grass", "Low load, humid climate grass"),
    "GR4": ("Grass", "Moderate load, dry climate grass"),
    "GR5": ("Grass", "Low load, humid climate grass"),
    "GR6": ("Grass", "Moderate load, humid climate grass"),
    "GR7": ("Grass", "High load, dry climate grass"),
    "GR8": ("Grass", "High load, humid climate grass"),
    "GR9": ("Grass", "Very high load, humid climate grass"),
    "GS1": ("Grass-Shrub", "Low load, dry climate"),
    "GS2": ("Grass-Shrub", "Moderate load, dry climate"),
    "GS3": ("Grass-Shrub", "Moderate load, humid climate"),
    "GS4": ("Grass-Shrub", "High load, humid climate"),
    "SH1": ("Shrub", "Low load, dry climate"),
    "SH2": ("Shrub", "Moderate load, dry climate"),
    "SH3": ("Shrub", "Moderate load, humid climate"),
    "SH4": ("Shrub", "Low load, humid climate"),
    "SH5": ("Shrub", "High load, dry climate"),
    "SH6": ("Shrub", "Low load, humid climate"),
    "SH7": ("Shrub", "Very high load, dry climate"),
    "SH8": ("Shrub", "High load, humid climate"),
    "SH9": ("Shrub", "Very high load, humid climate"),
    "TU1": ("Timber-Understory", "Low load, dry climate"),
    "TU2": ("Timber-Understory", "Moderate load, humid climate"),
    "TU3": ("Timber-Understory", "Moderate load, humid climate"),
    "TU4": ("Timber-Understory", "Dwarf conifer understory"),
    "TU5": ("Timber-Understory", "Very high load, dry climate"),
    "TL1": ("Timber Litter", "Low load, compact"),
    "TL2": ("Timber Litter", "Low load, broadleaf"),
    "TL3": ("Timber Litter", "Moderate load, conifer"),
    "TL4": ("Timber Litter", "Small downed logs"),
    "TL5": ("Timber Litter", "High load, conifer"),
    "TL6": ("Timber Litter", "Moderate load, hardwood"),
    "TL7": ("Timber Litter", "Large downed logs"),
    "TL8": ("Timber Litter", "Long-needle litter"),
    "TL9": ("Timber Litter", "Very high load, hardwood"),
    "SB1": ("Slash-Blowdown", "Low load"),
    "SB2": ("Slash-Blowdown", "Moderate load"),
    "SB3": ("Slash-Blowdown", "High load"),
    "SB4": ("Slash-Blowdown", "High load, continuous"),
}


class LandfireService:
    """Service for fetching LANDFIRE fuel model data.

    Uses COG backend when LANDFIRE_COG_URL is configured (faster, more scalable).
    Falls back to REST API otherwise.
    """

    def __init__(self):
        self.timeout = settings.http_timeout
        self._cog_service = get_landfire_cog_service()
        if self._cog_service:
            logger.info(f"LANDFIRE using COG backend: {settings.landfire_cog_url}")
        else:
            logger.info("LANDFIRE using REST API backend (no COG URL configured)")

    async def get_fuel_at_location(self, lat: float, lon: float) -> dict[str, Any]:
        """
        Get fuel model at a specific location.

        Args:
            lat: Latitude
            lon: Longitude

        Returns:
            Dict with fuel model code and description
        """
        # Use COG if available (faster)
        if self._cog_service:
            return self._query_cog(lat, lon)

        # Fall back to REST API
        return await self._query_rest_api(lat, lon)

    def _query_cog(self, lat: float, lon: float) -> dict[str, Any]:
        """Query fuel model from COG."""
        result = self._cog_service.point_query(lat, lon)

        if result["status"] != "success":
            return {
                "status": result["status"],
                "latitude": lat,
                "longitude": lon,
                "message": result.get("message", "COG query failed"),
            }

        # LANDFIRE FBFM40 pixel values are integers (91-204)
        pixel_value = result["value"]
        fuel_code = self._pixel_to_fuel_code(pixel_value)

        fuel_type, fuel_description = FUEL_MODELS.get(
            fuel_code, ("Unknown", "Unknown fuel model")
        )

        return {
            "status": "success",
            "latitude": lat,
            "longitude": lon,
            "fuel_model": {
                "code": fuel_code,
                "type": fuel_type,
                "description": fuel_description,
                "raw_value": pixel_value,
            },
            "source": "COG",
        }

    async def _query_rest_api(self, lat: float, lon: float) -> dict[str, Any]:
        """Query fuel model from LANDFIRE REST API (fallback)."""
        params = {
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "sr": "4326",  # WGS84
            "layers": "all:19",  # Layer 19 is FBFM40
            "tolerance": 2,
            "mapExtent": f"{lon-0.01},{lat-0.01},{lon+0.01},{lat+0.01}",
            "imageDisplay": "100,100,96",
            "returnGeometry": "false",
            "f": "json",
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    LANDFIRE_URL,
                    params=params,
                    timeout=self.timeout,
                )
                response.raise_for_status()

            data = response.json()
            results = data.get("results", [])

            if not results:
                return {
                    "status": "not_found",
                    "latitude": lat,
                    "longitude": lon,
                    "message": "No fuel data available for this location",
                }

            # Extract fuel model from first result
            attributes = results[0].get("attributes", {})
            pixel_value = attributes.get("Pixel Value", "")
            fuel_code = self._extract_fuel_code(pixel_value)

            fuel_type, fuel_description = FUEL_MODELS.get(
                fuel_code, ("Unknown", "Unknown fuel model")
            )

            return {
                "status": "success",
                "latitude": lat,
                "longitude": lon,
                "fuel_model": {
                    "code": fuel_code,
                    "type": fuel_type,
                    "description": fuel_description,
                    "raw_value": pixel_value,
                },
                "source": "REST",
            }

        except httpx.HTTPError as e:
            return {
                "status": "error",
                "latitude": lat,
                "longitude": lon,
                "message": f"LANDFIRE API error: {str(e)}",
            }

    def _pixel_to_fuel_code(self, pixel_value: int) -> str:
        """Convert LANDFIRE pixel value to fuel model code.

        FBFM40 codes:
        - 91-99: Non-burnable (NB1-NB9)
        - 101-109: Grass (GR1-GR9)
        - 121-124: Grass-Shrub (GS1-GS4)
        - 141-149: Shrub (SH1-SH9)
        - 161-165: Timber-Understory (TU1-TU5)
        - 181-189: Timber Litter (TL1-TL9)
        - 201-204: Slash-Blowdown (SB1-SB4)
        """
        pixel_to_code = {
            # Non-burnable
            91: "NB1",
            92: "NB2",
            93: "NB3",
            98: "NB8",
            99: "NB9",
            # Grass
            101: "GR1",
            102: "GR2",
            103: "GR3",
            104: "GR4",
            105: "GR5",
            106: "GR6",
            107: "GR7",
            108: "GR8",
            109: "GR9",
            # Grass-Shrub
            121: "GS1",
            122: "GS2",
            123: "GS3",
            124: "GS4",
            # Shrub
            141: "SH1",
            142: "SH2",
            143: "SH3",
            144: "SH4",
            145: "SH5",
            146: "SH6",
            147: "SH7",
            148: "SH8",
            149: "SH9",
            # Timber-Understory
            161: "TU1",
            162: "TU2",
            163: "TU3",
            164: "TU4",
            165: "TU5",
            # Timber Litter
            181: "TL1",
            182: "TL2",
            183: "TL3",
            184: "TL4",
            185: "TL5",
            186: "TL6",
            187: "TL7",
            188: "TL8",
            189: "TL9",
            # Slash-Blowdown
            201: "SB1",
            202: "SB2",
            203: "SB3",
            204: "SB4",
        }
        return pixel_to_code.get(pixel_value, f"Unknown({pixel_value})")

    def _extract_fuel_code(self, pixel_value: str) -> str:
        """Extract fuel model code from LANDFIRE pixel value."""
        # Pixel value format varies; try to extract the code
        if not pixel_value:
            return "Unknown"

        # Try direct lookup first
        value = pixel_value.strip().upper()
        if value in FUEL_MODELS:
            return value

        # Try extracting from descriptive text
        for code in FUEL_MODELS:
            if code in value:
                return code

        return value if value else "Unknown"


landfire_service = LandfireService()
