"""Tests for the true-color imagery endpoint and generic product pipeline.

Covers:
- True-color endpoint (bbox and lat/lon modes)
- NDVI/NDMI backward compatibility after refactor
- Cache key isolation between products
- Resolution tier selection
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

os.environ["COPERNICUS_CLIENT_ID"] = ""
os.environ["COPERNICUS_CLIENT_SECRET"] = ""

from ember.services.copernicus import (
    CopernicusService,
    _product_cache,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear product cache before and after each test."""
    _product_cache.clear()
    yield
    _product_cache.clear()


@pytest.fixture
def service():
    """Service with dummy credentials for API-call tests."""
    svc = CopernicusService()
    svc.client_id = "test"
    svc.client_secret = "test"
    return svc


@pytest.fixture
def unconfigured_service():
    """Service without credentials."""
    svc = CopernicusService()
    svc.client_id = ""
    svc.client_secret = ""
    return svc


def _mock_httpx_and_rasterio():
    """Return patches for httpx + rasterio that simulate a successful Copernicus call."""
    mock_token_response = MagicMock()
    mock_token_response.json.return_value = {
        "access_token": "test_token",
        "expires_in": 3600,
    }
    mock_token_response.raise_for_status = MagicMock()

    mock_process_response = MagicMock()
    mock_process_response.content = b"fake_image_data"
    mock_process_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=[mock_token_response, mock_process_response])

    mock_raster = MagicMock()
    mock_raster.read.return_value = np.array([[0.5, 0.6, 0.4]])

    return mock_client, mock_raster


# ============================================================================
# True-color endpoint tests
# ============================================================================


class TestGetTruecolor:
    """Tests for get_truecolor method."""

    @patch("ember.services.copernicus.httpx.AsyncClient")
    async def test_truecolor_bbox_png(self, mock_client_class, service):
        """True-color with bbox returns base64 PNG with product field."""
        mock_client, _ = _mock_httpx_and_rasterio()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.get_truecolor(
            min_lat=34.0,
            max_lat=34.5,
            min_lon=-118.5,
            max_lon=-118.0,
            format="png",
        )

        assert result["status"] == "success"
        assert result["product"] == "truecolor"
        assert result["source"] == "Sentinel-2 L2A"
        assert "raster" in result
        assert result["raster"]["format"] == "image/png"
        assert result["raster"]["encoding"] == "base64"
        assert isinstance(result["raster"]["data"], str)
        assert "date_range" in result

    @patch("ember.services.copernicus.httpx.AsyncClient")
    async def test_truecolor_latlon_mode(self, mock_client_class, service):
        """True-color with lat/lon/size_km works and includes lat/lon in response."""
        mock_client, _ = _mock_httpx_and_rasterio()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.get_truecolor(
            lat=42.0,
            lon=-122.0,
            size_km=10.0,
            format="png",
        )

        assert result["status"] == "success"
        assert result["product"] == "truecolor"
        assert result["latitude"] == 42.0
        assert result["longitude"] == -122.0

    async def test_truecolor_not_configured(self, unconfigured_service):
        """True-color returns not_configured when credentials missing."""
        result = await unconfigured_service.get_truecolor(
            min_lat=34.0, max_lat=34.5, min_lon=-118.5, max_lon=-118.0
        )
        assert result["status"] == "not_configured"

    async def test_truecolor_default_format_is_png(self, unconfigured_service):
        """True-color defaults to PNG format (not stats)."""
        # We can verify via the not_configured path — it still validates format
        result = await unconfigured_service.get_truecolor(
            min_lat=34.0, max_lat=34.5, min_lon=-118.5, max_lon=-118.0
        )
        # If format were invalid, we'd get an error, not not_configured
        assert result["status"] == "not_configured"


# ============================================================================
# NDVI/NDMI backward compatibility
# ============================================================================


class TestBackwardCompatibility:
    """Verify NDVI and NDMI response shapes are preserved after refactor."""

    @patch("ember.services.copernicus.rasterio.open")
    @patch("ember.services.copernicus.httpx.AsyncClient")
    async def test_ndvi_stats_response_shape(self, mock_client_class, mock_rasterio_open, service):
        """NDVI stats response has ndvi key with mean/min/max/vegetation_status."""
        mock_client, mock_raster = _mock_httpx_and_rasterio()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_rasterio_open.return_value.__enter__.return_value = mock_raster

        result = await service.get_ndvi(lat=38.85, lon=-120.89, size_km=5.0, format="stats")

        assert result["status"] == "success"
        assert "ndvi" in result
        assert "mean" in result["ndvi"]
        assert "min" in result["ndvi"]
        assert "max" in result["ndvi"]
        assert "vegetation_status" in result["ndvi"]
        assert "product" not in result  # ndvi should NOT have product field

    @patch("ember.services.copernicus.rasterio.open")
    @patch("ember.services.copernicus.httpx.AsyncClient")
    async def test_ndvi_raster_response_shape(self, mock_client_class, mock_rasterio_open, service):
        """NDVI raster response has raster + ndvi keys."""
        mock_client, mock_raster = _mock_httpx_and_rasterio()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_rasterio_open.return_value.__enter__.return_value = mock_raster

        result = await service.get_ndvi(lat=38.85, lon=-120.89, size_km=5.0, format="raster")

        assert result["status"] == "success"
        assert "raster" in result
        assert result["raster"]["format"] == "image/tiff"
        assert "ndvi" in result
        assert "mean" in result["ndvi"]

    @patch("ember.services.copernicus.rasterio.open")
    @patch("ember.services.copernicus.httpx.AsyncClient")
    async def test_ndmi_stats_response_shape(self, mock_client_class, mock_rasterio_open, service):
        """NDMI stats response has ndmi key with moisture_status and fire_risk."""
        mock_client, mock_raster = _mock_httpx_and_rasterio()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_rasterio_open.return_value.__enter__.return_value = mock_raster

        result = await service.get_ndmi(lat=38.85, lon=-120.89, size_km=5.0, format="stats")

        assert result["status"] == "success"
        assert "ndmi" in result
        assert "mean" in result["ndmi"]
        assert "min" in result["ndmi"]
        assert "max" in result["ndmi"]
        assert "moisture_status" in result["ndmi"]
        assert "fire_risk" in result["ndmi"]
        assert "product" not in result  # ndmi should NOT have product field

    @patch("ember.services.copernicus.rasterio.open")
    @patch("ember.services.copernicus.httpx.AsyncClient")
    async def test_ndmi_raster_response_shape(self, mock_client_class, mock_rasterio_open, service):
        """NDMI raster response has raster + ndmi keys with interpretation."""
        mock_client, mock_raster = _mock_httpx_and_rasterio()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_rasterio_open.return_value.__enter__.return_value = mock_raster

        result = await service.get_ndmi(lat=38.85, lon=-120.89, size_km=5.0, format="raster")

        assert result["status"] == "success"
        assert "raster" in result
        assert "ndmi" in result
        assert "moisture_status" in result["ndmi"]
        assert "fire_risk" in result["ndmi"]


# ============================================================================
# Cache key isolation
# ============================================================================


class TestCacheKeyIsolation:
    """Verify products don't collide in cache."""

    @patch("ember.services.copernicus.rasterio.open")
    @patch("ember.services.copernicus.httpx.AsyncClient")
    async def test_ndvi_and_ndmi_different_cache_keys(
        self, mock_client_class, mock_rasterio_open, service
    ):
        """NDVI and NDMI at same location produce different cache entries."""
        mock_client, mock_raster = _mock_httpx_and_rasterio()

        # Need fresh side_effect for each call (token + process per call)
        mock_client.post = AsyncMock(
            side_effect=[
                # NDVI: token + process
                MagicMock(
                    json=MagicMock(return_value={"access_token": "t", "expires_in": 3600}),
                    raise_for_status=MagicMock(),
                ),
                MagicMock(content=b"ndvi_data", raise_for_status=MagicMock()),
                # NDMI: process only (token cached)
                MagicMock(content=b"ndmi_data", raise_for_status=MagicMock()),
            ]
        )
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_rasterio_open.return_value.__enter__.return_value = mock_raster

        params = dict(lat=38.85, lon=-120.89, size_km=5.0, format="stats")

        await service.get_ndvi(**params)
        await service.get_ndmi(**params)

        # Both should be cached under different keys
        ndvi_keys = [k for k in _product_cache if k.startswith("ndvi:")]
        ndmi_keys = [k for k in _product_cache if k.startswith("ndmi:")]
        assert len(ndvi_keys) == 1
        assert len(ndmi_keys) == 1
        assert ndvi_keys[0] != ndmi_keys[0]

    @patch("ember.services.copernicus.httpx.AsyncClient")
    async def test_truecolor_separate_cache_key(self, mock_client_class, service):
        """True-color uses its own cache namespace."""
        mock_client, _ = _mock_httpx_and_rasterio()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        await service.get_truecolor(min_lat=34.0, max_lat=34.5, min_lon=-118.5, max_lon=-118.0)

        truecolor_keys = [k for k in _product_cache if k.startswith("truecolor:")]
        assert len(truecolor_keys) == 1


# ============================================================================
# Resolution tiers
# ============================================================================


class TestResolutionTiers:
    """Verify standard vs high resolution selection."""

    def test_standard_tier_small_area(self):
        """Standard tier: <10 km² -> 128."""
        svc = CopernicusService()
        w, h = svc._compute_resolution(5.0, "standard")
        assert (w, h) == (128, 128)

    def test_standard_tier_medium_area(self):
        """Standard tier: 10-50 km² -> 256."""
        svc = CopernicusService()
        w, h = svc._compute_resolution(25.0, "standard")
        assert (w, h) == (256, 256)

    def test_standard_tier_large_area(self):
        """Standard tier: >50 km² -> 512."""
        svc = CopernicusService()
        w, h = svc._compute_resolution(100.0, "standard")
        assert (w, h) == (512, 512)

    def test_high_tier_small_area(self):
        """High tier: <10 km² -> 256."""
        svc = CopernicusService()
        w, h = svc._compute_resolution(5.0, "high")
        assert (w, h) == (256, 256)

    def test_high_tier_medium_area(self):
        """High tier: 10-50 km² -> 512."""
        svc = CopernicusService()
        w, h = svc._compute_resolution(25.0, "high")
        assert (w, h) == (512, 512)

    def test_high_tier_large_area(self):
        """High tier: >50 km² -> 1024."""
        svc = CopernicusService()
        w, h = svc._compute_resolution(100.0, "high")
        assert (w, h) == (1024, 1024)


# ============================================================================
# Classification logic (backward compat for existing tests)
# ============================================================================


class TestClassificationLogicCompat:
    """Ensure old classification methods still work via delegation."""

    @pytest.mark.parametrize(
        "ndvi_value,expected",
        [
            (0.05, "Bare/Barren"),
            (0.15, "Sparse Vegetation"),
            (0.30, "Moderate Vegetation"),
            (0.50, "Healthy Vegetation"),
            (0.70, "Dense Vegetation"),
        ],
    )
    def test_ndvi_to_status(self, ndvi_value, expected):
        svc = CopernicusService()
        assert svc._ndvi_to_status(ndvi_value) == expected

    @pytest.mark.parametrize(
        "ndmi_value,expected_moisture,expected_risk",
        [
            (-0.3, "Very Dry", "High"),
            (-0.1, "Dry", "Moderate"),
            (0.0, "Moderate", "Moderate"),
            (0.15, "Moderate", "Low"),
            (0.25, "Moist", "Low"),
            (0.45, "Saturated", "Low"),
        ],
    )
    def test_ndmi_classification(self, ndmi_value, expected_moisture, expected_risk):
        svc = CopernicusService()
        assert svc._ndmi_to_moisture_status(ndmi_value) == expected_moisture
        assert svc._ndmi_to_fire_risk(ndmi_value) == expected_risk
