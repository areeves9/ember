"""Satellite imagery endpoints - Copernicus Sentinel-2 and direct COG access."""

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response

from ember.auth import require_auth
from ember.config import settings
from ember.services.copernicus import copernicus_service
from ember.services.sentinel_cog import sentinel_cog_service
from ember.services.stac import Scene, SceneQuery, stac_service

router = APIRouter(prefix="/imagery", tags=["imagery"])

# Browser cache TTL for full-extent Sentinel-2 previews (6 hours).
# Sentinel-2 NRT scenes publish every 3-5 days per tile, so this buffers
# a few requests within a user session without staling against real updates.
FULL_EXTENT_CACHE_SECONDS = 21600


def _validate_all_or_none_bbox(
    min_lat: float | None,
    max_lat: float | None,
    min_lon: float | None,
    max_lon: float | None,
) -> tuple[float, float, float, float] | None:
    """Enforce all-or-none bbox contract. Returns bbox tuple or None.

    Raises HTTPException(400) if some but not all bbox params are provided.
    """
    bbox_params = [min_lat, max_lat, min_lon, max_lon]
    provided = [p is not None for p in bbox_params]
    if all(provided):
        if min_lat >= max_lat:
            raise HTTPException(status_code=400, detail="min_lat must be less than max_lat")
        if min_lon >= max_lon:
            raise HTTPException(status_code=400, detail="min_lon must be less than max_lon")
        return (min_lon, min_lat, max_lon, max_lat)
    if not any(provided):
        return None
    names = ["min_lat", "max_lat", "min_lon", "max_lon"]
    missing = [n for n, p in zip(names, provided) if not p]
    raise HTTPException(
        status_code=400,
        detail=(
            f"Incomplete bbox: missing {', '.join(missing)}. "
            "Provide all four (min_lat, max_lat, min_lon, max_lon) or none."
        ),
    )


async def _find_single_best_scene(
    bbox: tuple[float, float, float, float],
    start_date: str | None,
    end_date: str | None,
    max_cloud_cover: float,
) -> tuple[Scene, str, str]:
    """Find the single most-recent cloud-free scene inside `bbox`.

    Used for the no-bbox full-extent path: STAC still requires a search
    region, so we seed with settings.sentinel_default_bbox (CONUS by default)
    and pick the single clearest recent scene inside it. The scene's own
    footprint becomes the effective output extent.
    """
    now = datetime.now(timezone.utc)
    if end_date is None:
        end_date = now.strftime("%Y-%m-%d")
    if start_date is None:
        start_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")

    query = SceneQuery(
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        max_cloud_cover=max_cloud_cover,
        limit=5,
    )
    try:
        scenes = await stac_service.search_scenes(query)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"STAC API error: {e}")
    if not scenes:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No Sentinel-2 scenes found in default region "
                f"{list(bbox)} between {start_date} and {end_date} "
                f"with cloud cover < {max_cloud_cover}%"
            ),
        )
    return scenes[0], start_date, end_date


@router.get("/truecolor")
async def get_truecolor(
    lat: Annotated[
        float | None,
        Query(ge=-90, le=90, description="Center latitude (use with lon and size_km)"),
    ] = None,
    lon: Annotated[
        float | None,
        Query(ge=-180, le=180, description="Center longitude (use with lat and size_km)"),
    ] = None,
    size_km: Annotated[
        float, Query(ge=1, le=100, description="Box size in km (use with lat/lon)")
    ] = 5.0,
    min_lat: Annotated[
        float | None,
        Query(ge=-90, le=90, description="South bound (use for bbox mode)"),
    ] = None,
    max_lat: Annotated[
        float | None,
        Query(ge=-90, le=90, description="North bound (use for bbox mode)"),
    ] = None,
    min_lon: Annotated[
        float | None,
        Query(ge=-180, le=180, description="West bound (use for bbox mode)"),
    ] = None,
    max_lon: Annotated[
        float | None,
        Query(ge=-180, le=180, description="East bound (use for bbox mode)"),
    ] = None,
    start_date: Annotated[
        str | None,
        Query(description="Start date YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    end_date: Annotated[
        str | None,
        Query(description="End date YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    format: Annotated[
        str, Query(description="Response format: 'png' (default) or 'raster' (GeoTIFF)")
    ] = "png",
    max_size: Annotated[
        int | None,
        Query(
            ge=64,
            le=2048,
            description="Override output resolution in pixels (default: auto by area)",
        ),
    ] = None,
    _user: dict = require_auth,
):
    """
    Get true-color RGB satellite imagery for a location.

    Returns a natural-color photograph from Sentinel-2 combining Red (B04),
    Green (B03), and Blue (B02) bands with 2.5x gain compensation.

    Uses high resolution tier (256/512/1024 pixels based on area).

    Usage:
    - Provide lat/lon/size_km for center-based queries
    - OR provide min_lat/max_lat/min_lon/max_lon for bbox queries
    - Default format is PNG (pre-rendered image, no GeoTIFF parsing needed)

    Data source: Copernicus Sentinel-2 L2A
    """
    if format not in ("png", "raster"):
        raise HTTPException(
            status_code=400,
            detail="Invalid format. Must be 'png' or 'raster'",
        )

    if min_lat is not None and max_lat is not None and min_lat >= max_lat:
        raise HTTPException(status_code=400, detail="min_lat must be less than max_lat")

    if min_lon is not None and max_lon is not None and min_lon >= max_lon:
        raise HTTPException(status_code=400, detail="min_lon must be less than max_lon")

    try:
        result = await copernicus_service.get_truecolor(
            lat=lat,
            lon=lon,
            size_km=size_km,
            min_lat=min_lat,
            max_lat=max_lat,
            min_lon=min_lon,
            max_lon=max_lon,
            start_date=start_date,
            end_date=end_date,
            format=format,
            max_size=max_size,
        )

        if result["status"] == "error":
            raise HTTPException(status_code=400, detail=result["message"])

        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Copernicus API error: {str(e)}")


@router.get("/truecolor-cog")
async def get_truecolor_cog(
    response: Response,
    min_lat: Annotated[
        float | None, Query(ge=-90, le=90, description="South bound (optional; all-or-none)")
    ] = None,
    max_lat: Annotated[
        float | None, Query(ge=-90, le=90, description="North bound (optional; all-or-none)")
    ] = None,
    min_lon: Annotated[
        float | None, Query(ge=-180, le=180, description="West bound (optional; all-or-none)")
    ] = None,
    max_lon: Annotated[
        float | None, Query(ge=-180, le=180, description="East bound (optional; all-or-none)")
    ] = None,
    start_date: Annotated[
        str | None,
        Query(
            description="Start date YYYY-MM-DD (default: 30 days ago)",
            pattern=r"^\d{4}-\d{2}-\d{2}$",
        ),
    ] = None,
    end_date: Annotated[
        str | None,
        Query(description="End date YYYY-MM-DD (default: today)", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    max_cloud_cover: Annotated[
        float, Query(ge=0, le=100, description="Max cloud cover percentage")
    ] = 20.0,
    max_size: Annotated[
        int | None,
        Query(
            ge=64,
            le=2048,
            description=(
                "Max pixel dimension (default 512 for bbox, "
                "overview_max_size for full-extent)"
            ),
        ),
    ] = None,
    format: Annotated[
        str, Query(description="Response format: 'png' (default) or 'raster' (GeoTIFF)")
    ] = "png",
    _user: dict = require_auth,
):
    """Get a true-color satellite photo via direct S3 COG access.

    Convenience endpoint that searches for the clearest recent Sentinel-2
    scene and returns an RGB composite (B04/B03/B02 with 2.5x gain).

    When bbox is omitted, returns the full native extent of the single
    most-recent cloud-free primary scene (via pyramid overviews). When
    bbox is provided, returns a bbox crop, stitching across MGRS tiles
    if needed.

    Data source: AWS sentinel-cogs S3 bucket via Earth Search STAC API.
    """
    if format not in ("png", "raster"):
        raise HTTPException(status_code=400, detail="format must be 'png' or 'raster'")

    bbox = _validate_all_or_none_bbox(min_lat, max_lat, min_lon, max_lon)

    if bbox is None:
        scene, start_date, end_date = await _find_single_best_scene(
            settings.sentinel_default_bbox, start_date, end_date, max_cloud_cover
        )
        effective_max_size = max_size if max_size is not None else settings.overview_max_size
        try:
            result = await sentinel_cog_service.get_truecolor(
                scene_id=scene.id,
                assets=scene.assets,
                bbox=None,
                max_size=effective_max_size,
                format=format,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"COG read error: {e}")

        result["datetime"] = scene.datetime
        result["cloud_cover"] = scene.cloud_cover
        result["scenes_used"] = 1
        response.headers["Cache-Control"] = f"public, max-age={FULL_EXTENT_CACHE_SECONDS}"
        return result

    scenes, start_date, end_date = await _find_coverage_scenes(
        bbox[1], bbox[3], bbox[0], bbox[2], start_date, end_date, max_cloud_cover
    )
    scene = scenes[0]
    effective_max_size = max_size if max_size is not None else 512

    try:
        result = await sentinel_cog_service.get_truecolor(
            scene_id=scene.id,
            assets=scene.assets,
            bbox=bbox,
            max_size=effective_max_size,
            format=format,
            scenes=scenes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"COG read error: {e}")

    result["datetime"] = scene.datetime
    result["cloud_cover"] = scene.cloud_cover
    result["scenes_used"] = len(scenes)
    return result


# ---------------------------------------------------------------------------
# Shared helper for COG convenience endpoints
# ---------------------------------------------------------------------------


async def _find_coverage_scenes(
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    start_date: str | None,
    end_date: str | None,
    max_cloud_cover: float,
) -> tuple[list[Scene], str, str]:
    """Search STAC for scenes covering the full bbox (best per MGRS tile).

    Returns (scenes, start_date, end_date).
    Raises HTTPException on validation failure or no results.
    """
    if min_lat >= max_lat:
        raise HTTPException(status_code=400, detail="min_lat must be less than max_lat")
    if min_lon >= max_lon:
        raise HTTPException(status_code=400, detail="min_lon must be less than max_lon")
    if (max_lat - min_lat) > 10 or (max_lon - min_lon) > 10:
        raise HTTPException(status_code=400, detail="Bbox too large (max 10 degrees per dimension)")

    now = datetime.now(timezone.utc)
    if end_date is None:
        end_date = now.strftime("%Y-%m-%d")
    if start_date is None:
        start_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")

    query = SceneQuery(
        bbox=(min_lon, min_lat, max_lon, max_lat),
        start_date=start_date,
        end_date=end_date,
        max_cloud_cover=max_cloud_cover,
    )

    try:
        scenes = await stac_service.search_coverage(query)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"STAC API error: {e}")

    if not scenes:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No Sentinel-2 scenes found for bbox "
                f"[{min_lon},{min_lat},{max_lon},{max_lat}] "
                f"between {start_date} and {end_date} "
                f"with cloud cover < {max_cloud_cover}%"
            ),
        )

    return scenes, start_date, end_date


# ---------------------------------------------------------------------------
# NDVI / NDMI interpretation (matches Copernicus thresholds exactly)
# ---------------------------------------------------------------------------


def _interpret_ndvi(stats: dict[str, float]) -> dict[str, Any]:
    mean = stats["mean"]
    if mean < 0.1:
        status = "Bare/Barren"
    elif mean < 0.2:
        status = "Sparse Vegetation"
    elif mean < 0.4:
        status = "Moderate Vegetation"
    elif mean < 0.6:
        status = "Healthy Vegetation"
    else:
        status = "Dense Vegetation"
    return {"vegetation_status": status}


def _interpret_ndmi(stats: dict[str, float]) -> dict[str, Any]:
    mean = stats["mean"]

    if mean < -0.2:
        moisture = "Very Dry"
    elif mean < 0.0:
        moisture = "Dry"
    elif mean < 0.2:
        moisture = "Moderate"
    elif mean < 0.4:
        moisture = "Moist"
    else:
        moisture = "Saturated"

    if mean < -0.1:
        risk = "High"
    elif mean < 0.1:
        risk = "Moderate"
    else:
        risk = "Low"

    return {"moisture_status": moisture, "fire_risk": risk}


# ---------------------------------------------------------------------------
# GET /imagery/ndvi-cog
# ---------------------------------------------------------------------------


@router.get("/ndvi-cog")
async def get_ndvi_cog(
    response: Response,
    min_lat: Annotated[
        float | None, Query(ge=-90, le=90, description="South bound (optional; all-or-none)")
    ] = None,
    max_lat: Annotated[
        float | None, Query(ge=-90, le=90, description="North bound (optional; all-or-none)")
    ] = None,
    min_lon: Annotated[
        float | None, Query(ge=-180, le=180, description="West bound (optional; all-or-none)")
    ] = None,
    max_lon: Annotated[
        float | None, Query(ge=-180, le=180, description="East bound (optional; all-or-none)")
    ] = None,
    start_date: Annotated[
        str | None,
        Query(
            description="Start date YYYY-MM-DD (default: 30 days ago)",
            pattern=r"^\d{4}-\d{2}-\d{2}$",
        ),
    ] = None,
    end_date: Annotated[
        str | None,
        Query(description="End date YYYY-MM-DD (default: today)", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    max_cloud_cover: Annotated[
        float, Query(ge=0, le=100, description="Max cloud cover percentage")
    ] = 20.0,
    max_size: Annotated[
        int | None, Query(ge=64, le=2048, description="Max pixel dimension")
    ] = None,
    format: Annotated[
        str, Query(description="Response format: 'stats' (default) or 'raster' (GeoTIFF + stats)")
    ] = "stats",
    _user: dict = require_auth,
):
    """Get NDVI via direct S3 COG access.

    Computes NDVI = (B08 - B04) / (B08 + B04) and includes vegetation_status.

    When bbox is omitted, returns the full native extent of the single
    most-recent cloud-free primary scene (via pyramid overviews). When
    bbox is provided, crops to the bbox and stitches across MGRS tiles.

    Data source: AWS sentinel-cogs S3 bucket via Earth Search STAC API.
    """
    if format not in ("stats", "raster"):
        raise HTTPException(status_code=400, detail="format must be 'stats' or 'raster'")

    bbox = _validate_all_or_none_bbox(min_lat, max_lat, min_lon, max_lon)

    if bbox is None:
        scene, start_date, end_date = await _find_single_best_scene(
            settings.sentinel_default_bbox, start_date, end_date, max_cloud_cover
        )
        effective_max_size = max_size if max_size is not None else settings.overview_max_size
        try:
            result = await sentinel_cog_service.compute_index(
                scene_id=scene.id,
                assets=scene.assets,
                index_name="ndvi",
                bbox=None,
                max_size=effective_max_size,
                format=format,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"COG read error: {e}")

        result["ndvi"] = {
            "mean": round(result["stats"]["mean"], 3),
            "min": round(result["stats"]["min"], 3),
            "max": round(result["stats"]["max"], 3),
            **_interpret_ndvi(result["stats"]),
        }
        result["date_range"] = {"start": start_date, "end": end_date}
        result["datetime"] = scene.datetime
        result["cloud_cover"] = scene.cloud_cover
        result["scenes_used"] = 1
        response.headers["Cache-Control"] = f"public, max-age={FULL_EXTENT_CACHE_SECONDS}"
        return result

    scenes, start_date, end_date = await _find_coverage_scenes(
        bbox[1], bbox[3], bbox[0], bbox[2], start_date, end_date, max_cloud_cover
    )
    scene = scenes[0]
    effective_max_size = max_size if max_size is not None else 512

    try:
        result = await sentinel_cog_service.compute_index(
            scene_id=scene.id,
            assets=scene.assets,
            index_name="ndvi",
            bbox=bbox,
            max_size=effective_max_size,
            format=format,
            scenes=scenes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"COG read error: {e}")

    result["ndvi"] = {
        "mean": round(result["stats"]["mean"], 3),
        "min": round(result["stats"]["min"], 3),
        "max": round(result["stats"]["max"], 3),
        **_interpret_ndvi(result["stats"]),
    }
    result["date_range"] = {"start": start_date, "end": end_date}
    result["datetime"] = scene.datetime
    result["cloud_cover"] = scene.cloud_cover
    result["scenes_used"] = len(scenes)
    return result


# ---------------------------------------------------------------------------
# GET /imagery/ndmi-cog
# ---------------------------------------------------------------------------


@router.get("/ndmi-cog")
async def get_ndmi_cog(
    response: Response,
    min_lat: Annotated[
        float | None, Query(ge=-90, le=90, description="South bound (optional; all-or-none)")
    ] = None,
    max_lat: Annotated[
        float | None, Query(ge=-90, le=90, description="North bound (optional; all-or-none)")
    ] = None,
    min_lon: Annotated[
        float | None, Query(ge=-180, le=180, description="West bound (optional; all-or-none)")
    ] = None,
    max_lon: Annotated[
        float | None, Query(ge=-180, le=180, description="East bound (optional; all-or-none)")
    ] = None,
    start_date: Annotated[
        str | None,
        Query(
            description="Start date YYYY-MM-DD (default: 30 days ago)",
            pattern=r"^\d{4}-\d{2}-\d{2}$",
        ),
    ] = None,
    end_date: Annotated[
        str | None,
        Query(description="End date YYYY-MM-DD (default: today)", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    max_cloud_cover: Annotated[
        float, Query(ge=0, le=100, description="Max cloud cover percentage")
    ] = 20.0,
    max_size: Annotated[
        int | None, Query(ge=64, le=2048, description="Max pixel dimension")
    ] = None,
    format: Annotated[
        str, Query(description="Response format: 'stats' (default) or 'raster' (GeoTIFF + stats)")
    ] = "stats",
    _user: dict = require_auth,
):
    """Get NDMI via direct S3 COG access.

    Computes NDMI = (B08 - B11) / (B08 + B11) and includes moisture_status
    and fire_risk interpretation.

    When bbox is omitted, returns the full native extent of the single
    most-recent cloud-free primary scene (via pyramid overviews). When
    bbox is provided, crops to the bbox and stitches across MGRS tiles.

    Data source: AWS sentinel-cogs S3 bucket via Earth Search STAC API.
    """
    if format not in ("stats", "raster"):
        raise HTTPException(status_code=400, detail="format must be 'stats' or 'raster'")

    bbox = _validate_all_or_none_bbox(min_lat, max_lat, min_lon, max_lon)

    if bbox is None:
        scene, start_date, end_date = await _find_single_best_scene(
            settings.sentinel_default_bbox, start_date, end_date, max_cloud_cover
        )
        effective_max_size = max_size if max_size is not None else settings.overview_max_size
        try:
            result = await sentinel_cog_service.compute_index(
                scene_id=scene.id,
                assets=scene.assets,
                index_name="ndmi",
                bbox=None,
                max_size=effective_max_size,
                format=format,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"COG read error: {e}")

        result["ndmi"] = {
            "mean": round(result["stats"]["mean"], 3),
            "min": round(result["stats"]["min"], 3),
            "max": round(result["stats"]["max"], 3),
            **_interpret_ndmi(result["stats"]),
        }
        result["date_range"] = {"start": start_date, "end": end_date}
        result["datetime"] = scene.datetime
        result["cloud_cover"] = scene.cloud_cover
        result["scenes_used"] = 1
        response.headers["Cache-Control"] = f"public, max-age={FULL_EXTENT_CACHE_SECONDS}"
        return result

    scenes, start_date, end_date = await _find_coverage_scenes(
        bbox[1], bbox[3], bbox[0], bbox[2], start_date, end_date, max_cloud_cover
    )
    scene = scenes[0]
    effective_max_size = max_size if max_size is not None else 512

    try:
        result = await sentinel_cog_service.compute_index(
            scene_id=scene.id,
            assets=scene.assets,
            index_name="ndmi",
            bbox=bbox,
            max_size=effective_max_size,
            format=format,
            scenes=scenes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"COG read error: {e}")

    result["ndmi"] = {
        "mean": round(result["stats"]["mean"], 3),
        "min": round(result["stats"]["min"], 3),
        "max": round(result["stats"]["max"], 3),
        **_interpret_ndmi(result["stats"]),
    }
    result["date_range"] = {"start": start_date, "end": end_date}
    result["datetime"] = scene.datetime
    result["cloud_cover"] = scene.cloud_cover
    result["scenes_used"] = len(scenes)
    return result
