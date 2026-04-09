"""Sentinel-2 scene discovery and COG band access endpoints."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from ember.auth import require_auth
from ember.services.sentinel_cog import INDEX_FORMULAS, sentinel_cog_service
from ember.services.stac import SceneQuery, stac_service

router = APIRouter(prefix="/scenes", tags=["scenes"])


def _validate_bbox(min_lat: float, max_lat: float, min_lon: float, max_lon: float) -> None:
    """Validate bbox ordering; raises HTTPException(400) on failure."""
    if min_lat >= max_lat:
        raise HTTPException(status_code=400, detail="min_lat must be less than max_lat")
    if min_lon >= max_lon:
        raise HTTPException(status_code=400, detail="min_lon must be less than max_lon")
    if (max_lat - min_lat) > 10 or (max_lon - min_lon) > 10:
        raise HTTPException(status_code=400, detail="Bbox too large (max 10 degrees per dimension)")


async def _resolve_scene(scene_id: str):
    """Look up a scene by ID, raising 404 if not found."""
    scene = await stac_service.get_scene(scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail=f"Scene not found: {scene_id}")
    return scene


@router.get("/search")
async def search_scenes(
    min_lon: Annotated[float, Query(ge=-180, le=180, description="West bound")],
    min_lat: Annotated[float, Query(ge=-90, le=90, description="South bound")],
    max_lon: Annotated[float, Query(ge=-180, le=180, description="East bound")],
    max_lat: Annotated[float, Query(ge=-90, le=90, description="North bound")],
    start_date: Annotated[
        str, Query(description="Start date YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$")
    ],
    end_date: Annotated[
        str, Query(description="End date YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$")
    ],
    max_cloud_cover: Annotated[
        float, Query(ge=0, le=100, description="Max cloud cover percentage")
    ] = 20.0,
    limit: Annotated[int, Query(ge=1, le=20, description="Max scenes to return")] = 5,
    _user: dict = require_auth,
):
    """Search for Sentinel-2 L2A scenes by location, date, and cloud cover.

    Returns scenes sorted by cloud cover (clearest first).
    Data source: Element 84 Earth Search STAC API.
    """
    _validate_bbox(min_lat, max_lat, min_lon, max_lon)

    if start_date > end_date:
        raise HTTPException(
            status_code=400, detail="start_date must be before or equal to end_date"
        )

    query = SceneQuery(
        bbox=(min_lon, min_lat, max_lon, max_lat),
        start_date=start_date,
        end_date=end_date,
        max_cloud_cover=max_cloud_cover,
        limit=limit,
    )

    try:
        scenes = await stac_service.search_scenes(query)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"STAC API error: {e}")

    return {
        "status": "success",
        "count": len(scenes),
        "scenes": [
            {
                "id": s.id,
                "datetime": s.datetime,
                "cloud_cover": s.cloud_cover,
                "bbox": list(s.bbox),
            }
            for s in scenes
        ],
    }


@router.get("/{scene_id}/bands")
async def get_scene_bands(
    scene_id: str,
    min_lon: Annotated[float, Query(ge=-180, le=180, description="West bound")],
    min_lat: Annotated[float, Query(ge=-90, le=90, description="South bound")],
    max_lon: Annotated[float, Query(ge=-180, le=180, description="East bound")],
    max_lat: Annotated[float, Query(ge=-90, le=90, description="North bound")],
    bands: Annotated[
        str, Query(description="Comma-separated band names, e.g. B04,B03,B02")
    ] = "B04,B03,B02",
    max_size: Annotated[int, Query(ge=64, le=2048, description="Max pixel dimension")] = 512,
    format: Annotated[
        str, Query(description="Response format: 'png' (default) or 'raster' (GeoTIFF)")
    ] = "png",
    _user: dict = require_auth,
):
    """Read band data from a specific Sentinel-2 scene.

    For RGB requests (B04,B03,B02), returns a true-color composite with 2.5x gain.
    For other band combos, returns a multi-band GeoTIFF.

    Data source: AWS sentinel-cogs S3 bucket (public, no credentials).
    """
    if format not in ("png", "raster"):
        raise HTTPException(status_code=400, detail="format must be 'png' or 'raster'")

    _validate_bbox(min_lat, max_lat, min_lon, max_lon)
    scene = await _resolve_scene(scene_id)

    band_list = [b.strip().upper() for b in bands.split(",")]
    bbox = (min_lon, min_lat, max_lon, max_lat)

    # True-color shortcut for RGB
    is_rgb = band_list == ["B04", "B03", "B02"]

    try:
        if is_rgb:
            result = await sentinel_cog_service.get_truecolor(
                scene_id=scene.id,
                assets=scene.assets,
                bbox=bbox,
                max_size=max_size,
                format=format,
            )
        else:
            band_data = await sentinel_cog_service.read_bands(
                assets=scene.assets,
                bands=band_list,
                bbox=bbox,
                max_size=max_size,
            )
            import numpy as np

            from ember.services.sentinel_cog import _encode_raster_geotiff

            stacked = np.stack([band_data[b] for b in band_list], axis=0)
            raster = _encode_raster_geotiff(stacked, bbox)
            result = {
                "status": "success",
                "scene_id": scene.id,
                "bbox": list(bbox),
                "bands": band_list,
                "raster": raster,
                "source": "Sentinel-2 L2A (AWS COG)",
            }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"COG read error: {e}")

    result["datetime"] = scene.datetime
    result["cloud_cover"] = scene.cloud_cover
    return result


@router.get("/{scene_id}/index")
async def get_scene_index(
    scene_id: str,
    index: Annotated[
        str,
        Query(description="Spectral index: ndvi, ndmi, nbr, or ndwi"),
    ],
    min_lon: Annotated[float, Query(ge=-180, le=180, description="West bound")],
    min_lat: Annotated[float, Query(ge=-90, le=90, description="South bound")],
    max_lon: Annotated[float, Query(ge=-180, le=180, description="East bound")],
    max_lat: Annotated[float, Query(ge=-90, le=90, description="North bound")],
    max_size: Annotated[int, Query(ge=64, le=2048, description="Max pixel dimension")] = 512,
    format: Annotated[
        str, Query(description="Response format: 'stats' or 'raster' (GeoTIFF + stats)")
    ] = "raster",
    _user: dict = require_auth,
):
    """Compute a spectral index for a specific Sentinel-2 scene.

    Supported indices: NDVI, NDMI, NBR, NDWI.
    Band math is performed in numpy on raw reflectance values.

    Data source: AWS sentinel-cogs S3 bucket (public, no credentials).
    """
    if format not in ("stats", "raster"):
        raise HTTPException(status_code=400, detail="format must be 'stats' or 'raster'")

    index_lower = index.lower()
    if index_lower not in INDEX_FORMULAS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown index '{index}'. Supported: {list(INDEX_FORMULAS.keys())}",
        )

    _validate_bbox(min_lat, max_lat, min_lon, max_lon)
    scene = await _resolve_scene(scene_id)

    bbox = (min_lon, min_lat, max_lon, max_lat)

    try:
        result = await sentinel_cog_service.compute_index(
            scene_id=scene.id,
            assets=scene.assets,
            index_name=index_lower,
            bbox=bbox,
            max_size=max_size,
            format=format,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"COG read error: {e}")

    result["datetime"] = scene.datetime
    result["cloud_cover"] = scene.cloud_cover
    return result
