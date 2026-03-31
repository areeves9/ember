"""National Weather Service fire weather alert service."""

from datetime import datetime, timezone
from time import time
from typing import Any

import httpx

from ember.config import settings
from ember.logging import get_logger

logger = get_logger(__name__)

NWS_BASE_URL = "https://api.weather.gov"
USER_AGENT = "Stellaris/Ember Fire Intelligence Platform (contact@stellaris.app)"

# Fire-relevant alert event types
FIRE_WEATHER_EVENTS = frozenset(
    {
        "Red Flag Warning",
        "Fire Weather Watch",
        "Extreme Fire Danger",
        "Fire Warning",
    }
)

# Cache: 15 minutes
_alert_cache: dict[str, dict] = {}
_ALERT_CACHE_TTL = 900  # 15 minutes
_ALERT_CACHE_MAX_SIZE = 200


class NWSService:
    """National Weather Service API client for fire weather products."""

    def __init__(self):
        self.timeout = settings.http_timeout
        self.headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/geo+json",
        }

    async def get_fire_weather_alerts(
        self,
        lat: float | None = None,
        lon: float | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """Get active Red Flag Warnings and Fire Weather Watches.

        Args:
            lat: Point query latitude
            lon: Point query longitude
            state: 2-letter state code

        Returns:
            Dict with filtered fire weather alerts and summary.
        """
        if lat is not None and lon is not None:
            mode = "point"
            cache_key = f"nws:alerts:{lat:.4f}:{lon:.4f}"
        elif state:
            mode = "state"
            cache_key = f"nws:alerts:state:{state.upper()}"
        else:
            raise ValueError("Must provide lat/lon or state")

        # Check cache
        cached = _alert_cache.get(cache_key)
        if cached and (time() - cached["timestamp"] < _ALERT_CACHE_TTL):
            return cached["data"]

        # Build NWS API request
        params: dict[str, str] = {"status": "actual"}
        if mode == "point":
            params["point"] = f"{lat},{lon}"
        elif mode == "state":
            params["area"] = state.upper()

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{NWS_BASE_URL}/alerts/active",
                params=params,
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()

        raw = response.json()
        features = raw.get("features", [])

        # Filter to fire-relevant events
        alerts = []
        for feature in features:
            props = feature.get("properties", {})
            event = props.get("event", "")
            if event not in FIRE_WEATHER_EVENTS:
                continue

            geometry = feature.get("geometry")

            # If no geometry, try to resolve from affected zones
            if geometry is None:
                zone_ids = props.get("affectedZones", [])
                geometry = await self._resolve_zone_geometry(zone_ids)

            alert = {
                "event": event,
                "severity": props.get("severity"),
                "urgency": props.get("urgency"),
                "headline": props.get("headline"),
                "description": props.get("description"),
                "instruction": props.get("instruction"),
                "onset": props.get("onset"),
                "expires": props.get("expires"),
                "issuing_office": props.get("senderName"),
                "affected_zones": props.get("affectedZones", []),
                "geometry": geometry,
                "parameters": self._extract_parameters(props.get("parameters", {})),
            }
            alerts.append(alert)

        # Count by type
        rfw_count = sum(1 for a in alerts if a["event"] == "Red Flag Warning")
        fww_count = sum(1 for a in alerts if a["event"] == "Fire Weather Watch")

        data = {
            "status": "success",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "alerts": alerts,
            "summary": {
                "red_flag_warnings": rfw_count,
                "fire_weather_watches": fww_count,
                "total_alerts": len(alerts),
            },
            "source": "NOAA/NWS",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }

        # Store in cache
        if len(_alert_cache) >= _ALERT_CACHE_MAX_SIZE:
            _alert_cache.clear()
        _alert_cache[cache_key] = {"timestamp": time(), "data": data}

        return data

    async def _resolve_zone_geometry(self, zone_urls: list[str]) -> dict[str, Any] | None:
        """Resolve NWS zone URLs to polygon geometry.

        Some alerts reference zones instead of inline geometry.
        Fetch zone details and merge polygons into a MultiPolygon.
        """
        if not zone_urls:
            return None

        polygons = []
        async with httpx.AsyncClient() as client:
            for zone_url in zone_urls:
                try:
                    resp = await client.get(
                        zone_url,
                        headers=self.headers,
                        timeout=self.timeout,
                    )
                    resp.raise_for_status()
                    zone_data = resp.json()
                    geom = zone_data.get("geometry")
                    if geom and geom.get("type") == "Polygon":
                        polygons.append(geom["coordinates"])
                    elif geom and geom.get("type") == "MultiPolygon":
                        polygons.extend(geom["coordinates"])
                except (httpx.HTTPError, KeyError):
                    logger.warning(f"Failed to resolve zone geometry: {zone_url}")
                    continue

        if not polygons:
            return None
        if len(polygons) == 1:
            return {"type": "Polygon", "coordinates": polygons[0]}
        return {"type": "MultiPolygon", "coordinates": polygons}

    def _extract_parameters(self, params: dict) -> dict[str, str | None]:
        """Extract fire weather parameters from NWS alert parameters."""
        return {
            "wind_speed_mph": self._first_or_none(params.get("windSpeed")),
            "wind_gusts_mph": self._first_or_none(params.get("windGust")),
            "min_relative_humidity_pct": self._first_or_none(params.get("minRelativeHumidity")),
        }

    @staticmethod
    def _first_or_none(val: Any) -> str | None:
        """Return first element if list, string if string, else None."""
        if isinstance(val, list) and val:
            return str(val[0])
        if isinstance(val, str):
            return val
        return None


nws_service = NWSService()
