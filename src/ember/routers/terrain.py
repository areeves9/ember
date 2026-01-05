"""Terrain endpoints - combined LANDFIRE layer queries."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from ember.auth import require_auth
from ember.services.terrain import get_terrain_service

router = APIRouter(prefix="/terrain", tags=["terrain"])


@router.get("")
async def get_terrain(
    lat: Annotated[float, Query(ge=-90, le=90, description="Latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Longitude")],
    layers: Annotated[str | None, Query(description="Comma-separated layer names (default: all)")] = None,
    # _user: dict = require_auth,  # TODO: Re-enable after testing
):
    """
    Get terrain data at a specific location.

    Returns combined data from multiple LANDFIRE layers for fire behavior modeling.
    Queries all available layers in parallel.

    Available layers:
    - fuel: FBFM40 fuel model code
    - slope: Slope in degrees
    - aspect: Aspect in degrees + cardinal direction
    - elevation: Elevation in meters
    - canopy_height: Canopy height in meters
    - canopy_base_height: Canopy base height in meters
    - canopy_bulk_density: Canopy bulk density in kg/m³
    - canopy_cover: Canopy cover percent
    """
    service = get_terrain_service()
    if not service:
        raise HTTPException(
            status_code=503,
            detail="Terrain service not configured (LANDFIRE_S3_PREFIX not set)",
        )

    # Parse layers parameter
    layer_list = None
    if layers:
        layer_list = [l.strip() for l in layers.split(",")]
        # Validate layer names
        invalid = set(layer_list) - set(service.available_layers)
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown layers: {invalid}. Available: {service.available_layers}",
            )

    try:
        result = await service.query_terrain(lat, lon, layer_list)
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Terrain query failed: {str(e)}")


@router.get("/layers")
async def list_layers():
    """List available terrain layers."""
    service = get_terrain_service()
    if not service:
        return {"available": False, "layers": [], "message": "Terrain service not configured"}

    return {
        "available": True,
        "layers": service.available_layers,
    }
