"""Sentinel-2 COG reader service.

Reads Sentinel-2 band COGs directly from the public AWS sentinel-cogs S3 bucket.
Supports multi-band composites (true-color RGB) and spectral index computation
(NDVI, NDMI, NBR, NDWI).

Follows the same ThreadPoolExecutor + rio-tiler pattern as TerrainService.
"""

import asyncio
import base64
import io
from concurrent.futures import ThreadPoolExecutor
from time import time
from typing import Any

import numpy as np
import rasterio
from PIL import Image
from rio_tiler.io import Reader
from scipy.ndimage import zoom

from ember.logging import get_logger

logger = get_logger(__name__)

# Spectral index definitions: name -> (band_a, band_b) for (A - B) / (A + B)
INDEX_FORMULAS: dict[str, tuple[str, str]] = {
    "ndvi": ("B08", "B04"),  # Vegetation
    "ndmi": ("B08", "B11"),  # Moisture
    "nbr": ("B08", "B12"),  # Burn ratio
    "ndwi": ("B03", "B08"),  # Water
}

# Band read cache: 24hr TTL (Sentinel-2 scenes are immutable)
_band_cache: dict[str, dict[str, Any]] = {}
_BAND_CACHE_TTL_SECONDS = 86400
_BAND_CACHE_MAX_SIZE = 100


def _band_cache_key(
    scene_id: str,
    bands: list[str],
    bbox: tuple[float, float, float, float],
    max_size: int,
) -> str:
    """Generate cache key for a band read, rounding bbox to 3 decimals."""

    def r(x: float) -> float:
        return round(x, 3)

    bbox_str = f"{r(bbox[0])},{r(bbox[1])},{r(bbox[2])},{r(bbox[3])}"
    bands_str = ",".join(sorted(bands))
    return f"s2:{scene_id}:{bands_str}:{bbox_str}:{max_size}"


def _get_cached_band_read(key: str) -> dict[str, Any] | None:
    entry = _band_cache.get(key)
    if not entry:
        return None
    if time() - entry["timestamp"] > _BAND_CACHE_TTL_SECONDS:
        del _band_cache[key]
        return None
    return entry["data"]


def _cache_band_read(key: str, data: dict[str, Any]) -> None:
    if len(_band_cache) >= _BAND_CACHE_MAX_SIZE:
        sorted_entries = sorted(_band_cache.items(), key=lambda x: x[1]["timestamp"])
        for key_to_remove, _ in sorted_entries[: len(sorted_entries) // 5]:
            del _band_cache[key_to_remove]
    _band_cache[key] = {"timestamp": time(), "data": data}


def _sentinel_env() -> rasterio.Env:
    """Rasterio env for unsigned S3 reads from the public sentinel-cogs bucket.

    Scoped via context manager — does NOT affect LANDFIRE pipeline's
    authenticated S3 access (rasterio.Env uses a thread-local stack).
    """
    return rasterio.Env(
        AWS_NO_SIGN_REQUEST="YES",
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff,.TIF,.TIFF",
        GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
        GDAL_HTTP_MULTIPLEX="YES",
        GDAL_HTTP_VERSION="2",
        VSI_CACHE="TRUE",
        VSI_CACHE_SIZE="5000000",
    )


def encode_raster_geotiff(
    data: np.ndarray,
    bbox: tuple[float, float, float, float],
    dtype: str = "float32",
) -> dict[str, Any]:
    """Encode numpy array as base64 GeoTIFF. Same pattern as terrain.py."""
    band_count = 1 if data.ndim == 2 else data.shape[0]
    height = data.shape[-2]
    width = data.shape[-1]

    min_lon, min_lat, max_lon, max_lat = bbox
    transform = rasterio.transform.from_bounds(min_lon, min_lat, max_lon, max_lat, width, height)

    buffer = io.BytesIO()
    with rasterio.open(
        buffer,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=band_count,
        dtype=dtype,
        crs="EPSG:4326",
        transform=transform,
        compress="lzw",
        nodata=0,
    ) as dst:
        if data.ndim == 2:
            dst.write(data, 1)
        else:
            for i in range(band_count):
                dst.write(data[i], i + 1)

    buffer.seek(0)
    b64_data = base64.b64encode(buffer.read()).decode("utf-8")

    return {
        "format": "geotiff",
        "encoding": "base64",
        "data": b64_data,
        "width": width,
        "height": height,
    }


def _encode_raster_png(data_rgb: np.ndarray) -> dict[str, Any]:
    """Encode 3-band uint8 array as base64 PNG."""
    # data_rgb shape: (3, H, W) -> (H, W, 3) for PIL
    img_array = np.moveaxis(data_rgb, 0, -1)
    img = Image.fromarray(img_array, mode="RGB")

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    b64_data = base64.b64encode(buffer.read()).decode("utf-8")

    return {
        "format": "image/png",
        "encoding": "base64",
        "data": b64_data,
        "width": img_array.shape[1],
        "height": img_array.shape[0],
    }


class SentinelCOGService:
    """Reads Sentinel-2 band COGs from the public AWS S3 bucket."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=8)

    def _read_band_sync(
        self,
        href: str,
        bbox: tuple[float, float, float, float],
        max_size: int,
    ) -> np.ndarray:
        """Read a single band COG within bbox (sync, runs in thread pool).

        rio-tiler handles UTM → EPSG:4326 reprojection transparently
        when the bbox is in geographic coordinates.
        """
        with _sentinel_env():
            with Reader(href) as src:
                img = src.part(
                    bbox=bbox,
                    max_size=max_size,
                    resampling_method="bilinear",
                )
                return img.data[0].astype(np.float32)  # Single band → 2D float array

    def _read_band_preview_sync(
        self,
        href: str,
        max_size: int,
    ) -> tuple[np.ndarray, tuple[float, float, float, float]]:
        """Read a full-scene preview of a band via overview selection.

        Returns (array, bbox_wgs84) where bbox is the scene's native bounds
        reprojected to EPSG:4326.
        """
        with _sentinel_env():
            with Reader(href) as src:
                img = src.preview(
                    max_size=max_size,
                    resampling_method="bilinear",
                    dst_crs="EPSG:4326",
                )
                bounds = img.bounds
                bbox_wgs84 = (bounds.left, bounds.bottom, bounds.right, bounds.top)
                return img.data[0].astype(np.float32), bbox_wgs84

    async def read_bands(
        self,
        assets: dict[str, str],
        bands: list[str],
        bbox: tuple[float, float, float, float],
        max_size: int = 512,
    ) -> dict[str, np.ndarray]:
        """Read multiple bands in parallel from a scene's assets."""
        missing = [b for b in bands if b not in assets]
        if missing:
            raise ValueError(f"Bands not available in scene assets: {missing}")

        loop = asyncio.get_running_loop()
        tasks = [
            loop.run_in_executor(self._executor, self._read_band_sync, assets[band], bbox, max_size)
            for band in bands
        ]
        results = await asyncio.gather(*tasks)
        return dict(zip(bands, results))

    async def read_bands_preview(
        self,
        assets: dict[str, str],
        bands: list[str],
        max_size: int,
    ) -> tuple[dict[str, np.ndarray], tuple[float, float, float, float]]:
        """Read full-scene previews for multiple bands in parallel.

        Returns (band_arrays, scene_bbox_wgs84). All bands share the scene's
        native footprint, so bbox comes from the first band read.
        """
        missing = [b for b in bands if b not in assets]
        if missing:
            raise ValueError(f"Bands not available in scene assets: {missing}")

        loop = asyncio.get_running_loop()
        tasks = [
            loop.run_in_executor(
                self._executor, self._read_band_preview_sync, assets[band], max_size
            )
            for band in bands
        ]
        results = await asyncio.gather(*tasks)
        arrays = {band: result[0] for band, result in zip(bands, results)}
        scene_bbox = results[0][1]  # All bands share the same scene footprint
        return arrays, scene_bbox

    async def read_bands_mosaic(
        self,
        scenes: list,
        bands: list[str],
        bbox: tuple[float, float, float, float],
        max_size: int = 512,
    ) -> tuple[dict[str, np.ndarray], tuple[float, float, float, float]]:
        """Read bands from multiple scenes and stitch into a single mosaic.

        Each scene is read at its own native MGRS tile bbox (not clipped to
        the requested viewport), then placed in a shared output canvas at
        its geographic offset within the union of all tile bboxes.

        Returns (mosaic, effective_bbox). For multi-scene mosaics, the
        effective bbox is the union of tile bboxes — slightly larger than
        the requested viewport, which gives clients a panning buffer for
        free. For the single-scene fast path, effective bbox equals the
        requested viewport.
        """
        # Drop scenes missing requested bands up front.
        valid_scenes = []
        for scene in scenes:
            missing = [b for b in bands if b not in scene.assets]
            if missing:
                logger.warning(f"Scene {scene.id} missing bands {missing}, skipping")
                continue
            valid_scenes.append(scene)

        if not valid_scenes:
            raise ValueError(f"No scenes contain all requested bands: {bands}")

        # Single scene → viewport-clip is correct, no mosaic alignment needed.
        if len(valid_scenes) == 1:
            bands_dict = await self.read_bands(valid_scenes[0].assets, bands, bbox, max_size)
            return bands_dict, bbox

        # Multi-scene: union of tile bboxes is the output extent.
        union_min_lon = min(s.bbox[0] for s in valid_scenes)
        union_min_lat = min(s.bbox[1] for s in valid_scenes)
        union_max_lon = max(s.bbox[2] for s in valid_scenes)
        union_max_lat = max(s.bbox[3] for s in valid_scenes)
        union_bbox = (union_min_lon, union_min_lat, union_max_lon, union_max_lat)

        logger.info(
            f"read_bands_mosaic: {len(valid_scenes)} tile(s), "
            f"viewport={tuple(round(v, 4) for v in bbox)}, "
            f"union={tuple(round(v, 4) for v in union_bbox)}"
        )
        for s in valid_scenes:
            logger.debug(
                f"  tile {s.mgrs_tile or s.id}: bbox={tuple(round(v, 4) for v in s.bbox)}"
            )

        # Output dims: longest side = max_size, preserve geographic aspect.
        union_w_deg = union_max_lon - union_min_lon
        union_h_deg = union_max_lat - union_min_lat
        aspect = union_w_deg / union_h_deg
        if aspect >= 1:
            out_w = max_size
            out_h = max(1, round(max_size / aspect))
        else:
            out_h = max_size
            out_w = max(1, round(max_size * aspect))

        def pixel_slot(s_bbox: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
            """Map a tile's geographic bbox to its (x0, y0, x1, y1) canvas slot."""
            smin_lon, smin_lat, smax_lon, smax_lat = s_bbox
            x0 = round((smin_lon - union_min_lon) / union_w_deg * out_w)
            x1 = round((smax_lon - union_min_lon) / union_w_deg * out_w)
            # Y is inverted: top of canvas = max_lat
            y0 = round((union_max_lat - smax_lat) / union_h_deg * out_h)
            y1 = round((union_max_lat - smin_lat) / union_h_deg * out_h)
            return max(0, x0), max(0, y0), min(out_w, x1), min(out_h, y1)

        # Read each (scene, band) at the scene's own bbox in parallel.
        # Per-tile max_size matches its slot to avoid wasted resolution.
        loop = asyncio.get_running_loop()
        read_tasks = []
        task_keys: list[tuple[int, str]] = []
        for scene_idx, scene in enumerate(valid_scenes):
            x0, y0, x1, y1 = pixel_slot(scene.bbox)
            slot_max = max(64, max(x1 - x0, y1 - y0))
            for band in bands:
                read_tasks.append(
                    loop.run_in_executor(
                        self._executor,
                        self._read_band_sync,
                        scene.assets[band],
                        scene.bbox,
                        slot_max,
                    )
                )
                task_keys.append((scene_idx, band))

        read_results = await asyncio.gather(*read_tasks, return_exceptions=True)

        band_layers: dict[str, list[tuple[int, np.ndarray]]] = {b: [] for b in bands}
        for (scene_idx, band_name), result in zip(task_keys, read_results):
            if isinstance(result, Exception):
                logger.warning(
                    f"Failed to read {band_name} from scene {valid_scenes[scene_idx].id}: {result}"
                )
                continue
            band_layers[band_name].append((scene_idx, result))

        mosaic: dict[str, np.ndarray] = {}
        for band_name in bands:
            layers = band_layers[band_name]
            if not layers:
                raise ValueError(f"No data for band {band_name} from any scene")

            canvas = np.zeros((out_h, out_w), dtype=np.float32)
            for scene_idx, arr in layers:
                x0, y0, x1, y1 = pixel_slot(valid_scenes[scene_idx].bbox)
                slot_h, slot_w = y1 - y0, x1 - x0
                if slot_h <= 0 or slot_w <= 0:
                    continue
                if arr.shape != (slot_h, slot_w):
                    arr = zoom(
                        arr,
                        (slot_h / arr.shape[0], slot_w / arr.shape[1]),
                        order=1,
                    )
                # First tile wins on overlap; canvas==0 sentinel preserves earlier fills.
                slot = canvas[y0:y1, x0:x1]
                mask = slot == 0
                slot[mask] = arr[mask]

            mosaic[band_name] = canvas

        return mosaic, union_bbox

    async def get_truecolor(
        self,
        scene_id: str,
        assets: dict[str, str],
        bbox: tuple[float, float, float, float] | None,
        max_size: int = 512,
        format: str = "png",
        scenes: list | None = None,
    ) -> dict[str, Any]:
        """Read RGB bands and compose true-color image with 2.5x gain.

        If `bbox` is None, reads the full scene via pyramid overviews
        (no stitching — single primary scene). If `scenes` is provided
        and `bbox` is set, reads from multiple scenes and stitches them
        into a mosaic for full bbox coverage.
        """
        if bbox is None:
            cache_key = f"{format}:preview:{scene_id}:B04,B03,B02:{max_size}"
            cached = _get_cached_band_read(cache_key)
            if cached:
                logger.debug(f"Band cache hit (preview): {cache_key}")
                return cached
            band_data, effective_bbox = await self.read_bands_preview(
                assets, ["B04", "B03", "B02"], max_size
            )
        else:
            scenes_key = "+".join(s.id for s in scenes) if scenes and len(scenes) > 1 else scene_id
            cache_key = (
                f"{format}:{_band_cache_key(scenes_key, ['B04', 'B03', 'B02'], bbox, max_size)}"
            )
            cached = _get_cached_band_read(cache_key)
            if cached:
                logger.debug(f"Band cache hit: {cache_key}")
                return cached

            if scenes and len(scenes) > 1:
                band_data, effective_bbox = await self.read_bands_mosaic(
                    scenes, ["B04", "B03", "B02"], bbox, max_size
                )
            else:
                band_data = await self.read_bands(assets, ["B04", "B03", "B02"], bbox, max_size)
                effective_bbox = bbox

        # Stack RGB (B04=Red, B03=Green, B02=Blue) and apply 2.5x gain
        rgb = np.stack([band_data["B04"], band_data["B03"], band_data["B02"]], axis=0)

        # Scale to uint8: Sentinel-2 L2A reflectance values are 0-10000
        # Apply 2.5x gain and clip to 0-255
        rgb_scaled = np.clip(rgb * 2.5 / 10000.0 * 255.0, 0, 255).astype(np.uint8)

        if format == "png":
            raster = _encode_raster_png(rgb_scaled)
        else:
            raster = encode_raster_geotiff(rgb_scaled, effective_bbox, dtype="uint8")

        result = {
            "status": "success",
            "scene_id": scene_id,
            "bbox": list(effective_bbox),
            "bands": ["B04", "B03", "B02"],
            "raster": raster,
            "source": "Sentinel-2 L2A (AWS COG)",
        }

        _cache_band_read(cache_key, result)
        return result

    async def compute_index(
        self,
        scene_id: str,
        assets: dict[str, str],
        index_name: str,
        bbox: tuple[float, float, float, float] | None,
        max_size: int = 512,
        format: str = "raster",
        scenes: list | None = None,
    ) -> dict[str, Any]:
        """Compute a spectral index from band math.

        If `bbox` is None, computes over the full scene via pyramid overviews
        (no stitching — single primary scene). If `scenes` is provided and
        `bbox` is set, reads from multiple scenes and stitches them into a
        mosaic for full bbox coverage.
        """
        index_name = index_name.lower()
        if index_name not in INDEX_FORMULAS:
            raise ValueError(
                f"Unknown index '{index_name}'. Supported: {list(INDEX_FORMULAS.keys())}"
            )

        band_a_name, band_b_name = INDEX_FORMULAS[index_name]

        if bbox is None:
            cache_key = (
                f"idx:{index_name}:{format}:preview:{scene_id}:"
                f"{band_a_name},{band_b_name}:{max_size}"
            )
            cached = _get_cached_band_read(cache_key)
            if cached:
                logger.debug(f"Index cache hit (preview): {index_name} {scene_id}")
                return cached
            band_data, effective_bbox = await self.read_bands_preview(
                assets, [band_a_name, band_b_name], max_size
            )
        else:
            scenes_key = "+".join(s.id for s in scenes) if scenes and len(scenes) > 1 else scene_id
            base_key = _band_cache_key(scenes_key, [band_a_name, band_b_name], bbox, max_size)
            cache_key = f"idx:{index_name}:{format}:{base_key}"
            cached = _get_cached_band_read(cache_key)
            if cached:
                logger.debug(f"Index cache hit: {index_name} {base_key}")
                return cached

            if scenes and len(scenes) > 1:
                band_data, effective_bbox = await self.read_bands_mosaic(
                    scenes, [band_a_name, band_b_name], bbox, max_size
                )
            else:
                band_data = await self.read_bands(
                    assets, [band_a_name, band_b_name], bbox, max_size
                )
                effective_bbox = bbox

        a = band_data[band_a_name]
        b = band_data[band_b_name]

        # Bands may have different native resolutions (e.g. B08=10m, B11=20m),
        # producing different array shapes. Resample the smaller to match the larger.
        if a.shape != b.shape:
            target_h = max(a.shape[0], b.shape[0])
            target_w = max(a.shape[1], b.shape[1])
            if a.shape[0] < target_h or a.shape[1] < target_w:
                a = zoom(a, (target_h / a.shape[0], target_w / a.shape[1]), order=1)
            if b.shape[0] < target_h or b.shape[1] < target_w:
                b = zoom(b, (target_h / b.shape[0], target_w / b.shape[1]), order=1)

        # Normalized difference: (A - B) / (A + B), avoiding division by zero
        denominator = a + b
        with np.errstate(invalid="ignore", divide="ignore"):
            index_values = np.where(
                denominator != 0,
                (a - b) / denominator,
                0.0,
            )

        # Compute stats on valid pixels
        valid = index_values[denominator != 0]
        stats = {
            "min": float(valid.min()) if valid.size > 0 else 0.0,
            "max": float(valid.max()) if valid.size > 0 else 0.0,
            "mean": float(valid.mean()) if valid.size > 0 else 0.0,
        }

        result: dict[str, Any] = {
            "status": "success",
            "scene_id": scene_id,
            "index": index_name.upper(),
            "bbox": list(effective_bbox),
            "bands_used": [band_a_name, band_b_name],
            "stats": stats,
            "source": "Sentinel-2 L2A (AWS COG)",
        }

        if format == "raster":
            result["raster"] = encode_raster_geotiff(index_values, effective_bbox)

        _cache_band_read(cache_key, result)
        return result


# Module-level singleton
sentinel_cog_service = SentinelCOGService()
