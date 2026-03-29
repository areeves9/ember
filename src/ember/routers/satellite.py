"""Satellite pass prediction endpoints."""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from ember.auth import require_auth
from ember.services.satellite import SATELLITE_REGISTRY, satellite_service

router = APIRouter(prefix="/satellite", tags=["satellite"])


@router.get("/next-pass")
async def get_next_pass(
    lat: Annotated[float, Query(ge=-90, le=90, description="Observer latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Observer longitude")],
    source: Annotated[
        str | None,
        Query(description="Satellite source key (e.g. VIIRS_SNPP_NRT). Omit for all."),
    ] = None,
    hours_ahead: Annotated[int, Query(ge=1, le=72, description="Prediction window in hours")] = 24,
    min_elevation: Annotated[
        float, Query(ge=0, le=90, description="Minimum peak elevation in degrees")
    ] = 10.0,
    _user: dict = require_auth,
):
    """
    Get upcoming satellite pass predictions for a location.

    Returns pass times (AOS/TCA/LOS), elevation, direction, sun angle,
    and quality score for fire-detection satellites over the given coordinates.

    If source is omitted, returns passes for all polar-orbiting satellites
    sorted by AOS. Geostationary sources return static refresh info.
    """
    try:
        if source is not None:
            # Single source query
            result = await satellite_service.get_passes(
                source=source,
                lat=lat,
                lon=lon,
                hours=hours_ahead,
                min_elevation=min_elevation,
            )
            result["location"] = {"lat": lat, "lon": lon}
            result["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return result

        # All polar-orbiting sources
        all_passes = []
        tle_stale = False

        for src_key, info in SATELLITE_REGISTRY.items():
            if info.is_geostationary:
                continue

            result = await satellite_service.get_passes(
                source=src_key,
                lat=lat,
                lon=lon,
                hours=hours_ahead,
                min_elevation=min_elevation,
            )
            if result.get("tle_stale"):
                tle_stale = True
            all_passes.extend(result.get("passes", []))

        all_passes.sort(key=lambda p: p["aos"])

        return {
            "location": {"lat": lat, "lon": lon},
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "all",
            "is_geostationary": False,
            "tle_stale": tle_stale,
            "prediction_window_hours": hours_ahead,
            "pass_count": len(all_passes),
            "passes": all_passes,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Satellite prediction error: {str(e)}")


@router.get("/sources")
async def list_sources():
    """List available satellite sources with orbital metadata."""
    sources = []
    for key, info in SATELLITE_REGISTRY.items():
        entry = {
            "id": key,
            "name": info.name,
            "instrument": info.instrument,
            "is_geostationary": info.is_geostationary,
        }
        if info.is_geostationary:
            entry["refresh_minutes"] = info.refresh_minutes
        else:
            entry["norad_ids"] = list(info.norad_ids)
            entry["swath_km"] = info.swath_km
        sources.append(entry)

    return {"sources": sources}
