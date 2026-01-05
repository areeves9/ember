"""Vegetation index endpoints - Copernicus Sentinel-2."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from ember.auth import require_auth
from ember.services.copernicus import copernicus_service

router = APIRouter(prefix="/vegetation", tags=["vegetation"])


@router.get("/ndvi")
async def get_ndvi(
    lat: Annotated[float, Query(ge=-90, le=90, description="Center latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Center longitude")],
    size_km: Annotated[float, Query(ge=1, le=50, description="Box size in km")] = 5.0,
    _user: dict = require_auth,
):
    """
    Get NDVI (Normalized Difference Vegetation Index) for a location.

    NDVI measures vegetation density and health.
    Range: -1 to +1 (higher = denser/healthier vegetation)

    Interpretation:
    - < 0.1: Bare ground, rock, sand, snow
    - 0.1-0.2: Sparse vegetation
    - 0.2-0.4: Moderate vegetation (shrubs, grassland)
    - 0.4-0.6: Healthy vegetation (crops, forests)
    - > 0.6: Dense vegetation (rainforest, peak growth)

    Data source: Copernicus Sentinel-2
    """
    try:
        result = await copernicus_service.get_ndvi(lat, lon, size_km)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Copernicus API error: {str(e)}")


@router.get("/ndmi")
async def get_ndmi(
    lat: Annotated[float, Query(ge=-90, le=90, description="Center latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Center longitude")],
    size_km: Annotated[float, Query(ge=1, le=50, description="Box size in km")] = 5.0,
    _user: dict = require_auth,
):
    """
    Get NDMI (Normalized Difference Moisture Index) for a location.

    NDMI measures vegetation water content - critical for fire risk assessment.
    Range: -1 to +1 (higher = more moisture)

    Fire Risk Correlation:
    - NDMI < -0.1: HIGH fire risk (dry fuels)
    - NDMI -0.1 to 0.1: MODERATE fire risk
    - NDMI > 0.1: LOW fire risk (moist fuels)

    Data source: Copernicus Sentinel-2
    """
    try:
        result = await copernicus_service.get_ndmi(lat, lon, size_km)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Copernicus API error: {str(e)}")
