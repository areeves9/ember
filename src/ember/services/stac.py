"""STAC scene discovery service for Sentinel-2 L2A imagery.

Uses Element 84 Earth Search to find Sentinel-2 scenes by location,
date range, and cloud cover. Returns scene metadata with S3 COG hrefs.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from time import time
from typing import Any

from pystac_client import Client as STACClient

from ember.config import settings
from ember.logging import get_logger

logger = get_logger(__name__)

COLLECTION = "sentinel-2-l2a"

# Bands we extract from STAC item assets
SENTINEL2_BANDS = [
    "B02",
    "B03",
    "B04",
    "B05",
    "B06",
    "B07",
    "B08",
    "B8A",
    "B11",
    "B12",
    "SCL",
]

# Scene search cache: 1 hour TTL
_search_cache: dict[str, dict[str, Any]] = {}
_SEARCH_CACHE_TTL_SECONDS = 3600
_SEARCH_CACHE_MAX_SIZE = 200

# Single-scene cache: 1 hour TTL
_scene_cache: dict[str, dict[str, Any]] = {}
_SCENE_CACHE_TTL_SECONDS = 3600
_SCENE_CACHE_MAX_SIZE = 500


@dataclass
class SceneQuery:
    """Parameters for STAC scene search."""

    bbox: tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat)
    start_date: str  # YYYY-MM-DD
    end_date: str  # YYYY-MM-DD
    max_cloud_cover: float = 20.0
    limit: int = 5


@dataclass
class Scene:
    """A discovered Sentinel-2 scene with band asset hrefs."""

    id: str
    datetime: str
    cloud_cover: float
    bbox: tuple[float, float, float, float]
    assets: dict[str, str] = field(default_factory=dict)  # band_name -> S3 href


def _search_cache_key(query: SceneQuery) -> str:
    """Generate cache key from query, rounding bbox to 3 decimals."""

    def r(x: float) -> float:
        return round(x, 3)

    bbox_str = f"{r(query.bbox[0])},{r(query.bbox[1])},{r(query.bbox[2])},{r(query.bbox[3])}"
    return (
        f"stac:{bbox_str}:{query.start_date}:{query.end_date}:{query.max_cloud_cover}:{query.limit}"
    )


def _get_cached_search(key: str) -> list[Scene] | None:
    entry = _search_cache.get(key)
    if not entry:
        return None
    if time() - entry["timestamp"] > _SEARCH_CACHE_TTL_SECONDS:
        del _search_cache[key]
        return None
    return entry["data"]


def _cache_search(key: str, data: list[Scene]) -> None:
    if len(_search_cache) >= _SEARCH_CACHE_MAX_SIZE:
        sorted_entries = sorted(_search_cache.items(), key=lambda x: x[1]["timestamp"])
        for key_to_remove, _ in sorted_entries[: len(sorted_entries) // 5]:
            del _search_cache[key_to_remove]
    _search_cache[key] = {"timestamp": time(), "data": data}


def _get_cached_scene(scene_id: str) -> Scene | None:
    entry = _scene_cache.get(scene_id)
    if not entry:
        return None
    if time() - entry["timestamp"] > _SCENE_CACHE_TTL_SECONDS:
        del _scene_cache[scene_id]
        return None
    return entry["data"]


def _cache_scene(scene: Scene) -> None:
    if len(_scene_cache) >= _SCENE_CACHE_MAX_SIZE:
        sorted_entries = sorted(_scene_cache.items(), key=lambda x: x[1]["timestamp"])
        for key_to_remove, _ in sorted_entries[: len(sorted_entries) // 5]:
            del _scene_cache[key_to_remove]
    _scene_cache[scene.id] = {"timestamp": time(), "data": scene}


def _item_to_scene(item: Any) -> Scene:
    """Convert a pystac Item to a Scene dataclass."""
    assets: dict[str, str] = {}
    for band in SENTINEL2_BANDS:
        asset = item.assets.get(band.lower()) or item.assets.get(band)
        if asset:
            assets[band] = asset.href

    cloud_cover = item.properties.get("eo:cloud_cover", 0.0)
    dt = item.properties.get("datetime", "")
    bbox = tuple(item.bbox) if item.bbox else (0.0, 0.0, 0.0, 0.0)

    return Scene(
        id=item.id,
        datetime=dt,
        cloud_cover=float(cloud_cover),
        bbox=bbox,
        assets=assets,
    )


class STACService:
    """Sentinel-2 scene discovery via Element 84 Earth Search STAC API."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._client: STACClient | None = None

    def _get_client(self) -> STACClient:
        """Lazy-initialize the STAC client."""
        if self._client is None:
            self._client = STACClient.open(settings.earth_search_url)
            logger.info(f"STAC client connected to {settings.earth_search_url}")
        return self._client

    def _search_sync(self, query: SceneQuery) -> list[Scene]:
        """Synchronous STAC search (runs in thread pool)."""
        client = self._get_client()

        datetime_range = f"{query.start_date}T00:00:00Z/{query.end_date}T23:59:59Z"

        search = client.search(
            collections=[COLLECTION],
            bbox=query.bbox,
            datetime=datetime_range,
            query={"eo:cloud_cover": {"lt": query.max_cloud_cover}},
            max_items=query.limit,
            sortby=[{"field": "properties.eo:cloud_cover", "direction": "asc"}],
        )

        scenes = [_item_to_scene(item) for item in search.items()]

        # Cache individual scenes for later lookup
        for scene in scenes:
            _cache_scene(scene)

        return scenes

    async def search_scenes(self, query: SceneQuery) -> list[Scene]:
        """Search for Sentinel-2 scenes matching the query."""
        cache_key = _search_cache_key(query)
        cached = _get_cached_search(cache_key)
        if cached is not None:
            logger.debug(f"STAC search cache hit: {cache_key}")
            return cached

        loop = asyncio.get_running_loop()
        scenes = await loop.run_in_executor(self._executor, self._search_sync, query)

        _cache_search(cache_key, scenes)
        logger.debug(f"STAC search returned {len(scenes)} scenes")
        return scenes

    def _get_scene_sync(self, scene_id: str) -> Scene | None:
        """Fetch a single scene by ID (runs in thread pool)."""
        client = self._get_client()
        try:
            search = client.search(
                collections=[COLLECTION],
                ids=[scene_id],
                max_items=1,
            )
            items = list(search.items())
            if not items:
                return None
            scene = _item_to_scene(items[0])
            _cache_scene(scene)
            return scene
        except Exception as e:
            logger.error(f"STAC scene fetch failed for {scene_id}: {e}")
            return None

    async def get_scene(self, scene_id: str) -> Scene | None:
        """Fetch a single scene by ID, checking cache first."""
        cached = _get_cached_scene(scene_id)
        if cached is not None:
            logger.debug(f"Scene cache hit: {scene_id}")
            return cached

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._get_scene_sync, scene_id)


# Module-level singleton
stac_service = STACService()
