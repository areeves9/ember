"""Fuel model endpoints - LANDFIRE."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from ember.auth import require_auth
from ember.services.landfire import FUEL_MODELS, landfire_service

router = APIRouter(prefix="/fuel", tags=["fuel"])


@router.get("")
async def get_fuel_model(
    lat: Annotated[float, Query(ge=-90, le=90, description="Latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Longitude")],
    _user: dict = require_auth,
):
    """
    Get fuel model at a specific location.

    Returns FBFM40 (40 Scott & Burgan Fire Behavior Fuel Models) classification.
    Data source: LANDFIRE (landfire.gov)
    """
    try:
        result = await landfire_service.get_fuel_at_location(lat, lon)
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LANDFIRE API error: {str(e)}")


@router.get("/models")
async def list_fuel_models():
    """List all FBFM40 fuel model codes and descriptions."""
    models = []
    for code, (fuel_type, description) in FUEL_MODELS.items():
        models.append(
            {
                "code": code,
                "type": fuel_type,
                "description": description,
            }
        )
    return {"models": models}
