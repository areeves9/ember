"""Terrain endpoints - combined LANDFIRE layer queries."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from ember.auth import require_auth
from ember.services.terrain import get_terrain_service

router = APIRouter(prefix="/terrain", tags=["terrain"])


@router.get("")
async def get_terrain(
    # Point mode parameters (existing)
    lat: Annotated[float | None, Query(ge=-90, le=90, description="Latitude (point mode)")] = None,
    lon: Annotated[float | None, Query(ge=-180, le=180, description="Longitude (point mode)")] = None,
    
    # Bbox mode parameters (new)
    min_lat: Annotated[float | None, Query(ge=-90, le=90, description="South boundary (bbox mode)")] = None,
    max_lat: Annotated[float | None, Query(ge=-90, le=90, description="North boundary (bbox mode)")] = None,
    min_lon: Annotated[float | None, Query(ge=-180, le=180, description="West boundary (bbox mode)")] = None,
    max_lon: Annotated[float | None, Query(ge=-180, le=180, description="East boundary (bbox mode)")] = None,
    
    # Common parameters
    layers: Annotated[
        str | None, Query(description="Comma-separated layer names (default: all for point, required for bbox raster)")
    ] = None,
    format: Annotated[str, Query(description="Response format: 'json' (default) or 'raster'")] = "json",
    # _user: dict = require_auth,  # TODO: Re-enable after testing
):
    """
    Get terrain data at a point or for a bounding box.

    Point mode: Provide lat and lon for scalar values.
    Bbox mode: Provide min_lat, max_lat, min_lon, max_lon for raster data.

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
    # Determine query mode
    has_point = lat is not None and lon is not None
    has_bbox = all(v is not None for v in [min_lat, max_lat, min_lon, max_lon])
    
    # Check for partial bbox parameters
    has_partial_bbox = any(v is not None for v in [min_lat, max_lat, min_lon, max_lon])

    if not has_point and not has_bbox:
        if has_partial_bbox:
            # Helpful error for incomplete bbox
            missing_params = []
            if min_lat is None: missing_params.append("min_lat")
            if max_lat is None: missing_params.append("max_lat")
            if min_lon is None: missing_params.append("min_lon")
            if max_lon is None: missing_params.append("max_lon")
            raise HTTPException(
                status_code=400,
                detail=f"Incomplete bbox: missing parameters {', '.join(missing_params)}. All four bbox parameters required.",
            )
        else:
            raise HTTPException(
                status_code=400,
                detail="Must provide either (lat, lon) for point query or (min_lat, max_lat, min_lon, max_lon) for bbox query",
            )

    if has_point and has_bbox:
        raise HTTPException(
            status_code=400,
            detail="Cannot mix point and bbox parameters. Use one or the other.",
        )

    service = get_terrain_service()
    if not service:
        raise HTTPException(
            status_code=503,
            detail="Terrain service not configured (LANDFIRE_S3_PREFIX not set)",
        )

    # Handle bbox raster query
    if has_bbox and format == "raster":
        if not layers:
            raise HTTPException(
                status_code=400,
                detail="Raster format requires exactly one layer. Provide 'layers' parameter.",
            )
        
        layer_list = [l.strip() for l in layers.split(",")]
        if len(layer_list) != 1:
            raise HTTPException(
                status_code=400,
                detail="Raster format supports exactly one layer at a time.",
            )
        
        layer = layer_list[0]
        
        # Validate layer name
        if layer not in service.available_layers:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown layer: {layer}. Available: {service.available_layers}",
            )
        
        try:
            result = await service.query_terrain_bbox_raster(
                min_lat=min_lat,
                max_lat=max_lat,
                min_lon=min_lon,
                max_lon=max_lon,
                layer=layer,
            )
            return result
        except Exception as e:
            # Note: Service layer returns generic error messages, so this is safe
            # If service layer changes, may want to log details without exposing to client
            raise HTTPException(status_code=502, detail=f"Terrain raster query failed: {str(e)}")

    # Handle bbox JSON query (stats only, no raster)
    if has_bbox and format == "json":
        # For JSON format with bbox, return stats for each requested layer
        layer_list = [l.strip() for l in layers.split(",")] if layers else service.available_layers
        
        # Validate layer names
        invalid = set(layer_list) - set(service.available_layers)
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown layers: {invalid}. Available: {service.available_layers}",
            )
        
        # For now, return error - we could implement stats-only bbox query later
        raise HTTPException(
            status_code=400,
            detail="Bbox queries currently only support format=raster. Use point query for JSON.",
        )

    # Original point query logic (unchanged)
    if has_point:
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
        return {
            "available": False,
            "layers": [],
            "message": "Terrain service not configured",
        }

    return {
        "available": True,
        "layers": service.available_layers,
    }
