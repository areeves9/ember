#!/usr/bin/env python3
"""Unit tests for Sentinel-2 COG reader service."""

import io
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import rasterio

from ember.services.sentinel_cog import (
    INDEX_FORMULAS,
    SentinelCOGService,
    _band_cache,
    _band_cache_key,
    encode_raster_geotiff,
)
from ember.services.stac import Scene

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def cog_service():
    """Provide a fresh SentinelCOGService instance."""
    return SentinelCOGService()


@pytest.fixture
def sample_assets():
    """Band asset hrefs for a test scene."""
    return {
        "B02": "s3://sentinel-cogs/path/B02.tif",
        "B03": "s3://sentinel-cogs/path/B03.tif",
        "B04": "s3://sentinel-cogs/path/B04.tif",
        "B08": "s3://sentinel-cogs/path/B08.tif",
        "B11": "s3://sentinel-cogs/path/B11.tif",
        "B12": "s3://sentinel-cogs/path/B12.tif",
    }


@pytest.fixture
def sample_bbox():
    return (-118.5, 34.0, -118.0, 34.5)


@pytest.fixture(autouse=True)
def clear_cache():
    _band_cache.clear()
    yield
    _band_cache.clear()


def _mock_reader(band_data: np.ndarray):
    """Create a mock rio-tiler Reader that returns the given array."""
    mock_img = MagicMock()
    mock_img.data = band_data.reshape(1, *band_data.shape) if band_data.ndim == 2 else band_data

    mock_reader_instance = MagicMock()
    mock_reader_instance.part.return_value = mock_img
    mock_reader_instance.__enter__ = MagicMock(return_value=mock_reader_instance)
    mock_reader_instance.__exit__ = MagicMock(return_value=False)
    return mock_reader_instance


# =============================================================================
# Cache key tests
# =============================================================================


class TestBandCacheKey:
    def test_same_inputs_produce_same_key(self):
        key1 = _band_cache_key("scene1", ["B04", "B03"], (-118.5, 34.0, -118.0, 34.5), 512)
        key2 = _band_cache_key("scene1", ["B04", "B03"], (-118.5, 34.0, -118.0, 34.5), 512)
        assert key1 == key2

    def test_band_order_normalized(self):
        key1 = _band_cache_key("scene1", ["B04", "B03"], (-118.5, 34.0, -118.0, 34.5), 512)
        key2 = _band_cache_key("scene1", ["B03", "B04"], (-118.5, 34.0, -118.0, 34.5), 512)
        assert key1 == key2

    def test_different_scene_different_key(self):
        key1 = _band_cache_key("scene1", ["B04"], (-118.5, 34.0, -118.0, 34.5), 512)
        key2 = _band_cache_key("scene2", ["B04"], (-118.5, 34.0, -118.0, 34.5), 512)
        assert key1 != key2

    def test_bbox_rounding(self):
        key1 = _band_cache_key("s1", ["B04"], (-118.500, 34.000, -118.000, 34.500), 512)
        key2 = _band_cache_key("s1", ["B04"], (-118.5001, 34.0004, -118.0002, 34.4999), 512)
        assert key1 == key2


# =============================================================================
# read_bands
# =============================================================================


class TestReadBands:
    @pytest.mark.asyncio
    async def test_reads_multiple_bands_in_parallel(self, cog_service, sample_assets, sample_bbox):
        band_array = np.ones((64, 64), dtype=np.float32) * 5000

        with (
            patch("ember.services.sentinel_cog.Reader") as MockReader,
            patch("ember.services.sentinel_cog._sentinel_env") as mock_env,
        ):
            mock_env.return_value.__enter__ = MagicMock()
            mock_env.return_value.__exit__ = MagicMock(return_value=False)
            MockReader.return_value = _mock_reader(band_array)

            result = await cog_service.read_bands(
                assets=sample_assets,
                bands=["B04", "B03", "B02"],
                bbox=sample_bbox,
                max_size=64,
            )

        assert set(result.keys()) == {"B04", "B03", "B02"}
        for band_name, data in result.items():
            assert data.shape == (64, 64)
            assert data.dtype == np.float32

    @pytest.mark.asyncio
    async def test_raises_on_missing_band(self, cog_service, sample_assets, sample_bbox):
        with pytest.raises(ValueError, match="Bands not available"):
            await cog_service.read_bands(
                assets=sample_assets,
                bands=["B99"],
                bbox=sample_bbox,
            )


# =============================================================================
# get_truecolor
# =============================================================================


class TestGetTruecolor:
    @pytest.mark.asyncio
    async def test_returns_success_with_raster(self, cog_service, sample_assets, sample_bbox):
        band_array = np.ones((64, 64), dtype=np.float32) * 3000

        with (
            patch("ember.services.sentinel_cog.Reader") as MockReader,
            patch("ember.services.sentinel_cog._sentinel_env") as mock_env,
        ):
            mock_env.return_value.__enter__ = MagicMock()
            mock_env.return_value.__exit__ = MagicMock(return_value=False)
            MockReader.return_value = _mock_reader(band_array)

            result = await cog_service.get_truecolor(
                scene_id="S2A_TEST",
                assets=sample_assets,
                bbox=sample_bbox,
                max_size=64,
                format="raster",
            )

        assert result["status"] == "success"
        assert result["scene_id"] == "S2A_TEST"
        assert result["bands"] == ["B04", "B03", "B02"]
        assert result["raster"]["format"] == "geotiff"
        assert result["raster"]["encoding"] == "base64"
        assert result["raster"]["width"] == 64
        assert result["raster"]["height"] == 64

    @pytest.mark.asyncio
    async def test_caches_result(self, cog_service, sample_assets, sample_bbox):
        band_array = np.ones((64, 64), dtype=np.float32) * 3000

        with (
            patch("ember.services.sentinel_cog.Reader") as MockReader,
            patch("ember.services.sentinel_cog._sentinel_env") as mock_env,
        ):
            mock_env.return_value.__enter__ = MagicMock()
            mock_env.return_value.__exit__ = MagicMock(return_value=False)
            MockReader.return_value = _mock_reader(band_array)

            # First call
            await cog_service.get_truecolor(
                scene_id="S2A_TEST",
                assets=sample_assets,
                bbox=sample_bbox,
                max_size=64,
                format="raster",
            )
            # Second call should use cache
            result = await cog_service.get_truecolor(
                scene_id="S2A_TEST",
                assets=sample_assets,
                bbox=sample_bbox,
                max_size=64,
                format="raster",
            )

        assert result["status"] == "success"
        # Reader should only be constructed for the first call's 3 bands
        assert MockReader.call_count == 3


# =============================================================================
# compute_index
# =============================================================================


class TestComputeIndex:
    @pytest.mark.asyncio
    async def test_ndvi_computation(self, cog_service, sample_assets, sample_bbox):
        # B08 (NIR) = 8000, B04 (Red) = 2000 -> NDVI = (8000-2000)/(8000+2000) = 0.6
        nir = np.ones((64, 64), dtype=np.float32) * 8000
        red = np.ones((64, 64), dtype=np.float32) * 2000

        call_count = 0

        def mock_read_sync(href, bbox, max_size):
            nonlocal call_count
            call_count += 1
            if "B08" in href:
                return nir
            return red

        with patch.object(cog_service, "_read_band_sync", side_effect=mock_read_sync):
            result = await cog_service.compute_index(
                scene_id="S2A_TEST",
                assets=sample_assets,
                index_name="ndvi",
                bbox=sample_bbox,
                max_size=64,
            )

        assert result["status"] == "success"
        assert result["index"] == "NDVI"
        assert abs(result["stats"]["mean"] - 0.6) < 0.01
        assert result["bands_used"] == ["B08", "B04"]

    @pytest.mark.asyncio
    async def test_handles_resolution_mismatch(self, cog_service, sample_assets, sample_bbox):
        """NDMI mixes B08 (10m, larger array) and B11 (20m, smaller array)."""
        nir_10m = np.ones((160, 298), dtype=np.float32) * 8000  # B08 at 10m
        swir_20m = np.ones((80, 149), dtype=np.float32) * 2000  # B11 at 20m

        def mock_read_sync(href, bbox, max_size):
            if "B08" in href:
                return nir_10m
            return swir_20m

        with patch.object(cog_service, "_read_band_sync", side_effect=mock_read_sync):
            result = await cog_service.compute_index(
                scene_id="S2A_TEST",
                assets=sample_assets,
                index_name="ndmi",
                bbox=sample_bbox,
                max_size=512,
            )

        assert result["status"] == "success"
        assert result["index"] == "NDMI"
        # (8000 - 2000) / (8000 + 2000) = 0.6
        assert abs(result["stats"]["mean"] - 0.6) < 0.01

    @pytest.mark.asyncio
    async def test_handles_zero_denominator(self, cog_service, sample_assets, sample_bbox):
        # Both bands zero -> denominator is 0, should return 0 (not NaN/inf)
        zeros = np.zeros((64, 64), dtype=np.float32)

        with patch.object(cog_service, "_read_band_sync", return_value=zeros):
            result = await cog_service.compute_index(
                scene_id="S2A_TEST",
                assets=sample_assets,
                index_name="ndvi",
                bbox=sample_bbox,
                max_size=64,
            )

        assert result["status"] == "success"
        assert result["stats"]["mean"] == 0.0

    @pytest.mark.asyncio
    async def test_rejects_unknown_index(self, cog_service, sample_assets, sample_bbox):
        with pytest.raises(ValueError, match="Unknown index"):
            await cog_service.compute_index(
                scene_id="S2A_TEST",
                assets=sample_assets,
                index_name="fake_index",
                bbox=sample_bbox,
            )

    def test_all_index_formulas_have_two_bands(self):
        for name, (band_a, band_b) in INDEX_FORMULAS.items():
            assert band_a.startswith("B"), f"{name}: band_a should start with B"
            assert band_b.startswith("B"), f"{name}: band_b should start with B"
            assert band_a != band_b, f"{name}: bands should differ"


# =============================================================================
# read_bands_mosaic (multi-scene stitching)
# =============================================================================


def _make_scene(scene_id: str, mgrs_tile: str, assets: dict[str, str]) -> Scene:
    return Scene(
        id=scene_id,
        datetime="2026-03-15T18:00:00Z",
        cloud_cover=5.0,
        bbox=(-118.5, 34.0, -118.0, 34.5),
        mgrs_tile=mgrs_tile,
        assets=assets,
    )


class TestReadBandsMosaic:
    @pytest.mark.asyncio
    async def test_single_scene_delegates_to_read_bands(self, cog_service, sample_bbox):
        """With one scene, mosaic should just call read_bands."""
        scene = _make_scene(
            "S2A_11SLT",
            "11SLT",
            {
                "B04": "s3://path/B04.tif",
                "B03": "s3://path/B03.tif",
            },
        )
        band_array = np.ones((64, 64), dtype=np.float32) * 5000

        with patch.object(
            cog_service,
            "read_bands",
            return_value={
                "B04": band_array,
                "B03": band_array,
            },
        ) as mock_rb:
            result = await cog_service.read_bands_mosaic([scene], ["B04", "B03"], sample_bbox, 64)

        mock_rb.assert_called_once()
        assert set(result.keys()) == {"B04", "B03"}

    @pytest.mark.asyncio
    async def test_two_scenes_stitched(self, cog_service, sample_bbox):
        """Two scenes with complementary data should fill the full canvas."""
        scene_a = _make_scene("S2A_11SLT", "11SLT", {"B04": "s3://a/B04.tif"})
        scene_b = _make_scene("S2B_11SLU", "11SLU", {"B04": "s3://b/B04.tif"})

        # Scene A covers top half, scene B covers bottom half
        top_half = np.zeros((64, 64), dtype=np.float32)
        top_half[:32, :] = 5000.0

        bottom_half = np.zeros((64, 64), dtype=np.float32)
        bottom_half[32:, :] = 3000.0

        call_count = 0

        def mock_read_sync(href, bbox, max_size):
            nonlocal call_count
            call_count += 1
            if "a/" in href:
                return top_half
            return bottom_half

        with patch.object(cog_service, "_read_band_sync", side_effect=mock_read_sync):
            result = await cog_service.read_bands_mosaic(
                [scene_a, scene_b], ["B04"], sample_bbox, 64
            )

        canvas = result["B04"]
        assert canvas.shape == (64, 64)
        # Top half should have scene A data
        assert canvas[0, 0] == 5000.0
        # Bottom half should have scene B data
        assert canvas[63, 0] == 3000.0
        # No zeros left
        assert np.all(canvas > 0)

    @pytest.mark.asyncio
    async def test_skips_scene_with_missing_bands(self, cog_service, sample_bbox):
        """Scene missing a requested band should be skipped, not error."""
        scene_ok = _make_scene("S2A_OK", "11SLT", {"B04": "s3://ok/B04.tif"})
        scene_bad = _make_scene("S2B_BAD", "11SLU", {})  # No bands

        data = np.ones((64, 64), dtype=np.float32) * 5000

        with patch.object(cog_service, "_read_band_sync", return_value=data):
            result = await cog_service.read_bands_mosaic(
                [scene_ok, scene_bad], ["B04"], sample_bbox, 64
            )

        assert result["B04"].shape == (64, 64)

    @pytest.mark.asyncio
    async def test_truecolor_with_mosaic(self, cog_service, sample_bbox):
        """get_truecolor with multiple scenes should use mosaic path."""
        scenes = [
            _make_scene(
                "S2A_11SLT",
                "11SLT",
                {
                    "B04": "s3://a/B04.tif",
                    "B03": "s3://a/B03.tif",
                    "B02": "s3://a/B02.tif",
                },
            ),
            _make_scene(
                "S2B_11SLU",
                "11SLU",
                {
                    "B04": "s3://b/B04.tif",
                    "B03": "s3://b/B03.tif",
                    "B02": "s3://b/B02.tif",
                },
            ),
        ]
        band_data = {
            "B04": np.ones((64, 64), dtype=np.float32) * 3000,
            "B03": np.ones((64, 64), dtype=np.float32) * 3000,
            "B02": np.ones((64, 64), dtype=np.float32) * 3000,
        }

        with patch.object(cog_service, "read_bands_mosaic", return_value=band_data) as mock_mosaic:
            result = await cog_service.get_truecolor(
                scene_id="S2A_11SLT",
                assets=scenes[0].assets,
                bbox=sample_bbox,
                max_size=64,
                format="raster",
                scenes=scenes,
            )

        mock_mosaic.assert_called_once()
        assert result["status"] == "success"


# =============================================================================
# encode_raster_geotiff — nodata tag (ORQ-141 Phase 2)
# =============================================================================


SAMPLE_BBOX = (-118.5, 34.0, -118.0, 34.5)


class TestEncodeRasterGeotiffNodata:
    """Verify that encode_raster_geotiff always writes nodata=0 into the GeoTIFF profile.

    Phase 2 of ORQ-141: without this tag, renderers treat zero-valued pixels
    (the mosaic nodata sentinel) as opaque rather than transparent.
    """

    def _decode(self, result: dict) -> rasterio.DatasetReader:
        """Decode a base64 GeoTIFF result dict back into a rasterio dataset."""
        import base64

        raw = base64.b64decode(result["data"])
        return rasterio.open(io.BytesIO(raw))

    def test_float32_single_band_has_nodata_zero(self):
        """1-band float32 output (spectral index) carries nodata=0."""
        data = np.zeros((32, 32), dtype=np.float32)
        data[5:10, 5:10] = 0.6  # some non-zero pixels

        result = encode_raster_geotiff(data, SAMPLE_BBOX, dtype="float32")

        with self._decode(result) as ds:
            assert ds.nodata == 0, f"Expected nodata=0, got {ds.nodata}"

    def test_uint8_three_band_has_nodata_zero(self):
        """3-band uint8 output (truecolor raster) carries nodata=0."""
        data = np.zeros((3, 32, 32), dtype=np.uint8)
        data[:, 5:10, 5:10] = 128  # some non-zero RGB pixels

        result = encode_raster_geotiff(data, SAMPLE_BBOX, dtype="uint8")

        with self._decode(result) as ds:
            assert ds.nodata == 0, f"Expected nodata=0, got {ds.nodata}"

    def test_nodata_zero_present_when_array_is_all_zeros(self):
        """All-zero array (fully uncovered mosaic gap) still has nodata=0 tag."""
        data = np.zeros((16, 16), dtype=np.float32)

        result = encode_raster_geotiff(data, SAMPLE_BBOX, dtype="float32")

        with self._decode(result) as ds:
            assert ds.nodata == 0

    def test_nodata_zero_present_when_array_has_no_zeros(self):
        """Array with no zero pixels still carries the nodata=0 tag."""
        data = np.ones((16, 16), dtype=np.float32) * 0.75

        result = encode_raster_geotiff(data, SAMPLE_BBOX, dtype="float32")

        with self._decode(result) as ds:
            assert ds.nodata == 0
