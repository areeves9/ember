"""OpenStreetMap Overpass API service for infrastructure proximity queries.

Queries the Overpass API for industrial facilities, military installations,
pipelines, power plants, and roads within a bounding box. Results are cached
aggressively (24h) since infrastructure doesn't relocate.

Designed as a general-purpose OSM query service — facility proximity for FIRMS
clusters is the first use case, but the same service supports future queries
(structure fires, land use, population density, etc.).
"""

import math
from time import time
from typing import Any

import httpx

from ember.logging import get_logger

logger = get_logger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_OVERPASS_TIMEOUT = 10.0  # seconds — Overpass queries are bounded by [timeout:N]

# Cache for Overpass results (infrastructure is static)
_overpass_cache: dict[str, dict] = {}
_OVERPASS_CACHE_TTL = 86400  # 24 hours
_OVERPASS_CACHE_MAX_SIZE = 100

# Cooldown after rate limit or failure — skip queries for this duration
_overpass_last_failure: float = 0.0
_overpass_cooldown_logged: bool = False
_OVERPASS_COOLDOWN = 300  # 5 minutes

# Earth radius in km
EARTH_RADIUS_KM = 6371.0

# OSM tag → simplified facility type
FACILITY_TYPE_MAP = {
    "petroleum_well": "oil_well",
    "pipeline": "pipeline",
    "oil": "refinery",
    "gas": "gas_processing",
    "refinery": "refinery",
    "power_plant": "power_plant",
    "military": "military",
    "industrial": "industrial_generic",
    "waste_disposal": "waste_disposal",
}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in km between two points."""
    rlat1, rlon1 = math.radians(lat1), math.radians(lon1)
    rlat2, rlon2 = math.radians(lat2), math.radians(lon2)
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(a))


def _classify_facility(tags: dict[str, str]) -> str | None:
    """Map OSM tags to a simplified facility type."""
    # Check specific tags in priority order
    if tags.get("man_made") == "petroleum_well":
        return "oil_well"
    if tags.get("man_made") == "pipeline":
        return "pipeline"
    industrial = tags.get("industrial", "")
    if industrial in ("oil", "gas", "refinery"):
        return FACILITY_TYPE_MAP.get(industrial, "industrial_generic")
    if tags.get("power") == "plant":
        return "power_plant"
    if "military" in tags:
        return "military"
    if tags.get("amenity") == "waste_disposal":
        return "waste_disposal"
    if tags.get("landuse") == "industrial":
        return "industrial_generic"
    return None


class OverpassService:
    """Service for querying OpenStreetMap infrastructure via Overpass API."""

    def __init__(self):
        self.timeout = _OVERPASS_TIMEOUT

    async def query_infrastructure(
        self,
        min_lat: float,
        max_lat: float,
        min_lon: float,
        max_lon: float,
        pad_km: float = 10.0,
    ) -> dict[str, list[dict[str, Any]]]:
        """Query Overpass for facilities and roads in a bbox.

        Args:
            min_lat: South boundary
            max_lat: North boundary
            min_lon: West boundary
            max_lon: East boundary
            pad_km: Padding in km beyond bbox (default 10)

        Returns:
            Dict with "facilities" and "roads" lists. Each entry has
            lat, lon, type, and optionally name.
        """
        # Pad bbox
        pad_deg = pad_km / 111.32
        s = min_lat - pad_deg
        n = max_lat + pad_deg
        w = min_lon - pad_deg
        e = max_lon + pad_deg

        global _overpass_last_failure, _overpass_cooldown_logged

        # Cache check (round to 1 decimal ~11km)
        cache_key = f"overpass:{s:.1f},{n:.1f},{w:.1f},{e:.1f}"
        cached = _overpass_cache.get(cache_key)
        if cached and (time() - cached["timestamp"] < _OVERPASS_CACHE_TTL):
            return cached["data"]

        # Cooldown: skip queries after rate limit or failure
        if _overpass_last_failure and (time() - _overpass_last_failure < _OVERPASS_COOLDOWN):
            return {"facilities": [], "roads": []}

        bbox = f"{s},{w},{n},{e}"
        query = f"""
[out:json][timeout:10];
(
  node["man_made"="petroleum_well"]({bbox});
  way["landuse"="industrial"]({bbox});
  way["military"]({bbox});
  node["power"="plant"]({bbox});
  way["power"="plant"]({bbox});
  way["industrial"~"oil|gas|refinery"]({bbox});
  node["man_made"="pipeline"]({bbox});
  way["man_made"="pipeline"]({bbox});
  node["amenity"="waste_disposal"]({bbox});
  way["highway"~"motorway|trunk|primary|secondary"]({bbox});
);
out center;
"""

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    OVERPASS_URL,
                    data={"data": query},
                    timeout=self.timeout,
                )
                resp.raise_for_status()

            data = resp.json()
            result = self._parse_overpass_response(data)

            # Cache
            if len(_overpass_cache) >= _OVERPASS_CACHE_MAX_SIZE:
                _overpass_cache.clear()
            _overpass_cache[cache_key] = {
                "timestamp": time(),
                "data": result,
            }

            # Reset cooldown on success
            if _overpass_last_failure:
                logger.info("Overpass recovered")
                _overpass_last_failure = 0.0
                _overpass_cooldown_logged = False

            fac_count = len(result["facilities"])
            road_count = len(result["roads"])
            logger.info(
                "Overpass query: %d facilities, %d road segments",
                fac_count,
                road_count,
            )
            return result

        except Exception as exc:
            _overpass_last_failure = time()
            if not _overpass_cooldown_logged:
                _overpass_cooldown_logged = True
                logger.warning(
                    "Overpass unavailable — cooldown active for %ds. "
                    "Facility enrichment will return nulls. Error: %s",
                    _OVERPASS_COOLDOWN,
                    exc,
                )
            return {"facilities": [], "roads": []}

    @staticmethod
    def _parse_overpass_response(
        data: dict,
    ) -> dict[str, list[dict[str, Any]]]:
        """Parse Overpass JSON response into facilities and roads."""
        facilities: list[dict[str, Any]] = []
        roads: list[dict[str, Any]] = []

        for element in data.get("elements", []):
            tags = element.get("tags", {})

            # Get coordinates (nodes have lat/lon, ways have center)
            lat = element.get("lat") or element.get("center", {}).get("lat")
            lon = element.get("lon") or element.get("center", {}).get("lon")
            if lat is None or lon is None:
                continue

            # Check if it's a road
            if "highway" in tags:
                roads.append({"lat": lat, "lon": lon})
                continue

            # Classify as facility
            facility_type = _classify_facility(tags)
            if facility_type:
                entry: dict[str, Any] = {
                    "lat": lat,
                    "lon": lon,
                    "type": facility_type,
                }
                name = tags.get("name")
                if name:
                    entry["name"] = name
                facilities.append(entry)

        return {"facilities": facilities, "roads": roads}

    @staticmethod
    def find_nearest_facilities(
        lat: float,
        lon: float,
        facilities: list[dict[str, Any]],
        max_km: float = 50.0,
        top_n: int = 3,
    ) -> dict[str, Any]:
        """Find the nearest facilities to a point.

        Returns dict with:
        - nearest: {km, type, name} for the closest facility (or None)
        - nearby: top N facilities sorted by distance, each with
          {name, type, distance_km}. Empty list if none within max_km.
        """
        scored = []
        for fac in facilities:
            dist = _haversine_km(lat, lon, fac["lat"], fac["lon"])
            if dist < max_km:
                scored.append((fac, dist))

        scored.sort(key=lambda x: x[1])

        nearby = [
            {
                "name": fac.get("name"),
                "type": fac["type"],
                "distance_km": round(dist, 2),
            }
            for fac, dist in scored[:top_n]
        ]

        if not scored:
            return {"nearest": None, "nearby": []}

        closest_fac, closest_dist = scored[0]
        return {
            "nearest": {
                "km": round(closest_dist, 2),
                "type": closest_fac["type"],
                "name": closest_fac.get("name"),
            },
            "nearby": nearby,
        }

    @staticmethod
    def find_nearest_road(
        lat: float,
        lon: float,
        roads: list[dict[str, Any]],
        max_km: float = 50.0,
    ) -> float | None:
        """Find distance to nearest road segment center point.

        Returns distance in km, or None if nothing within max_km.
        """
        nearest_dist = max_km

        for road in roads:
            dist = _haversine_km(lat, lon, road["lat"], road["lon"])
            if dist < nearest_dist:
                nearest_dist = dist

        if nearest_dist >= max_km:
            return None

        return round(nearest_dist, 2)


overpass_service = OverpassService()
