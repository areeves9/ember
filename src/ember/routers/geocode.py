"""Geocoding endpoints - Nominatim (OpenStreetMap)."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from ember.auth import require_auth
from ember.services.nominatim import nominatim_service

router = APIRouter(prefix="/geocode", tags=["geocode"])


@router.get("/forward")
async def geocode_address(
    address: Annotated[str, Query(min_length=1, description="Address to geocode")],
    country: Annotated[str | None, Query(description="ISO country code filter")] = None,
    _user: dict = require_auth,
):
    """
    Convert an address to coordinates.

    Uses OpenStreetMap Nominatim API.
    """
    try:
        result = await nominatim_service.geocode(address, country)
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Geocoding error: {str(e)}")


@router.get("/reverse")
async def reverse_geocode(
    lat: Annotated[float, Query(ge=-90, le=90, description="Latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Longitude")],
    zoom: Annotated[int, Query(ge=0, le=18, description="Detail level")] = 18,
    _user: dict = require_auth,
):
    """
    Convert coordinates to an address.

    Uses OpenStreetMap Nominatim API.
    Zoom levels: 0=country, 10=city, 14=suburb, 16=street, 18=building
    """
    try:
        result = await nominatim_service.reverse_geocode(lat, lon, zoom)
        return result
    except Exception as e:
        raise HTTPException(
            status_code=502, detail=f"Reverse geocoding error: {str(e)}"
        )
