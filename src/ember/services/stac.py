"""STAC scene discovery service for Sentinel-2 L2A imagery.

Uses Element 84 Earth Search to find Sentinel-2 scenes by location,
date range, and cloud cover. Returns scene metadata with S3 COG hrefs.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from time import time
from typing import Any

import mgrs as _mgrs_lib
from pystac_client import Client as STACClient

from ember.config import settings
from ember.logging import get_logger

logger = get_logger(__name__)

COLLECTION = "sentinel-2-l2a"

# Mapping from canonical Sentinel-2 band names to Earth Search STAC asset keys.
# Earth Search uses common names (red, nir, etc.) not band IDs (B04, B08).
# B10 (Cirrus) is intentionally omitted — not available in L2A products.
BAND_TO_STAC_KEY: dict[str, str] = {
    "B01": "coastal",
    "B02": "blue",
    "B03": "green",
    "B04": "red",
    "B05": "rededge1",
    "B06": "rededge2",
    "B07": "rededge3",
    "B08": "nir",
    "B8A": "nir08",
    "B09": "nir09",
    "B11": "swir16",
    "B12": "swir22",
    "SCL": "scl",
}

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
    mgrs_tile: str = ""  # e.g. "11SLT" — extracted from scene ID
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


def _intersecting_mgrs_tiles(bbox: tuple[float, float, float, float]) -> set[str]:
    """Compute the set of MGRS 100 km grid square IDs that intersect a bbox.

    bbox is (min_lon, min_lat, max_lon, max_lat) in WGS84.
    Returns canonical Sentinel-2 tile IDs (e.g. "10TES"), uppercase, no spaces.

    Uses a sampling grid with step ~0.4° (~44 km at mid-latitudes — smaller than
    one 100 km MGRS tile) to ensure every tile inside the bbox is captured.  The
    four corners are always sampled so narrow bboxes still return at least one tile.
    Over-inclusion is harmless; under-inclusion is not.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    m = _mgrs_lib.MGRS()
    tiles: set[str] = set()

    step = 0.4

    # Walk the grid
    lat = min_lat
    while lat <= max_lat:
        lon = min_lon
        while lon <= max_lon:
            tiles.add(m.toMGRS(lat, lon, MGRSPrecision=0))
            lon += step
        # Always include the right edge
        tiles.add(m.toMGRS(lat, max_lon, MGRSPrecision=0))
        lat += step
    # Always include the top edge
    lon = min_lon
    while lon <= max_lon:
        tiles.add(m.toMGRS(max_lat, lon, MGRSPrecision=0))
        lon += step
    tiles.add(m.toMGRS(max_lat, max_lon, MGRSPrecision=0))

    return tiles


def _item_to_scene(item: Any) -> Scene:
    """Convert a pystac Item to a Scene dataclass.

    Maps Earth Search common-name asset keys (red, nir, swir16, ...)
    back to canonical Sentinel-2 band names (B04, B08, B11, ...) so
    the rest of the codebase can use band IDs consistently.
    """
    assets: dict[str, str] = {}
    for band, stac_key in BAND_TO_STAC_KEY.items():
        asset = item.assets.get(stac_key)
        if asset:
            assets[band] = asset.href

    cloud_cover = item.properties.get("eo:cloud_cover", 0.0)
    dt = item.properties.get("datetime", "")
    bbox = tuple(item.bbox) if item.bbox else (0.0, 0.0, 0.0, 0.0)

    # Extract MGRS tile from scene ID (e.g. "S2A_11SLT_20260315_0_L2A" → "11SLT")
    mgrs_tile = ""
    parts = item.id.split("_")
    if len(parts) >= 2:
        mgrs_tile = parts[1]

    return Scene(
        id=item.id,
        datetime=dt,
        cloud_cover=float(cloud_cover),
        bbox=bbox,
        mgrs_tile=mgrs_tile,
        assets=assets,
    )


def pick_best_per_tile(scenes: list[Scene]) -> list[Scene]:
    """Select the clearest scene for each MGRS tile.

    When a bbox spans multiple tiles, STAC returns multiple scenes per tile
    (different dates). This picks the lowest cloud cover per tile so we get
    full spatial coverage with the best quality.
    """
    best: dict[str, Scene] = {}
    for scene in scenes:
        key = scene.mgrs_tile or scene.id
        if key not in best or scene.cloud_cover < best[key].cloud_cover:
            best[key] = scene
    return list(best.values())


class STACService:
    """Sentinel-2 scene discovery via Element 84 Earth Search STAC API."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._client: STACClient | None = None
        self._client_lock = threading.Lock()

    def _get_client(self) -> STACClient:
        """Lazy-initialize the STAC client (thread-safe)."""
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = STACClient.open(settings.earth_search_url)
                    logger.info("STAC client connected")
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

    async def search_coverage(self, query: SceneQuery) -> list[Scene]:
        """Search for scenes and return the best one per MGRS tile.

        Implements a progressive search loop driven by
        ``settings.sentinel_search_windows`` (default [30, 60, 90] days).
        The loop widens the look-back window step by step until every MGRS tile
        that geographically intersects the requested bbox has a best-scene
        assigned, or the window list is exhausted.  Results from all windows are
        merged so that tiles covered by the narrowest window keep their
        fresher/clearer scene while tiles not yet covered pick up candidates from
        wider windows.

        The required tile set is computed locally before any STAC query fires,
        using the ``mgrs`` library to sample the bbox at 0.4° intervals and
        resolve each sample to its containing 100 km MGRS grid square.  This
        means a tile that has zero scenes within the full 90-day window is
        correctly detected as missing and named in the exhaustion warning, rather
        than silently absent.

        Fetches up to 20 scenes per window — enough for bboxes spanning up to
        ~4-6 MGRS tiles with multiple date options per tile.
        """
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        end_date = query.end_date

        accumulated_scenes: list[Scene] = []
        best_per_tile: dict[str, Scene] = {}

        # Pre-compute the required tile set from the bbox geometry before any
        # STAC query fires (Option A).  A tile absent from every window's results
        # is now detectable and named in the exhaustion warning.
        required_tiles: set[str] = _intersecting_mgrs_tiles(query.bbox)

        for window_days in settings.sentinel_search_windows:
            start_date = (now - timedelta(days=window_days)).strftime("%Y-%m-%d")

            window_query = SceneQuery(
                bbox=query.bbox,
                start_date=start_date,
                end_date=end_date,
                max_cloud_cover=100.0,  # Accept all scenes — cloudy fill beats no fill
                limit=20,
            )
            window_scenes = await self.search_scenes(window_query)

            # Merge: union of all scenes seen so far.  Earlier-window (fresher)
            # scenes appear first in the list; pick_best_per_tile keeps the
            # lowest cloud_cover per tile, so a later-window scene only displaces
            # an earlier one when it is clearer.
            accumulated_scenes = accumulated_scenes + window_scenes
            new_best = pick_best_per_tile(accumulated_scenes)
            best_per_tile = {s.mgrs_tile or s.id: s for s in new_best}

            covered_tile_ids = set(best_per_tile.keys())
            uncovered = required_tiles - covered_tile_ids

            if not uncovered:
                logger.info(
                    f"search_coverage: {window_days}-day window sufficient "
                    f"for {len(covered_tile_ids)} tile(s)"
                )
                break

            logger.debug(
                f"search_coverage: {window_days}-day window — "
                f"{len(uncovered)} tile(s) still uncovered — escalating"
            )
        else:
            # Loop exhausted without break — some tiles remain uncovered.
            uncovered = required_tiles - set(best_per_tile.keys())
            if uncovered:
                logger.warning(
                    f"search_coverage: exhausted all search windows "
                    f"({settings.sentinel_search_windows} days); "
                    f"no scene found for tile(s): {sorted(uncovered)}"
                )
            else:
                logger.info(
                    f"search_coverage: all {len(best_per_tile)} known tile(s) covered "
                    f"after exhausting search windows"
                )

        return list(best_per_tile.values())

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
