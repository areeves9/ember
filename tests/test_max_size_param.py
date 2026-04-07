"""Tests for max_size parameter across terrain, vegetation, and imagery endpoints.

Covers:
- max_size overrides area-based resolution heuristic (Copernicus)
- max_size passes through to terrain service
- Omitting max_size preserves current behavior (backward compat)
- max_size is included in cache key (no collisions between resolutions)
- max_size validation (out-of-range values)
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
    """Service with dummy credentials."""
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


def _mock_copernicus_call():
    """Return mock httpx client that simulates successful Copernicus call."""
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
    return mock_client


# ============================================================================
# Copernicus pipeline: max_size overrides resolution
# ============================================================================


class TestMaxSizeResolutionOverride:
    """Verify max_size overrides the area-based resolution heuristic."""

    @patch("ember.services.copernicus.rasterio.open")
    @patch("ember.services.copernicus.httpx.AsyncClient")
    async def test_ndvi_with_max_size_uses_requested_resolution(
        self, mock_client_class, mock_rasterio_open, service
    ):
        """NDVI with max_size=1024 produces 1024x1024 in raster response."""
        mock_client = _mock_copernicus_call()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_raster = MagicMock()
        mock_raster.read.return_value = np.array([[0.5, 0.6, 0.4]])
        mock_rasterio_open.return_value.__enter__.return_value = mock_raster

        result = await service.get_ndvi(
            min_lat=42.0,
            max_lat=42.5,
            min_lon=-122.0,
            max_lon=-121.5,
            format="raster",
            max_size=1024,
        )

        assert result["status"] == "success"
        assert result["raster"]["width"] == 1024
        assert result["raster"]["height"] == 1024

    @patch("ember.services.copernicus.rasterio.open")
    @patch("ember.services.copernicus.httpx.AsyncClient")
    async def test_ndvi_without_max_size_uses_area_heuristic(
        self, mock_client_class, mock_rasterio_open, service
    ):
        """NDVI without max_size falls back to area-based resolution."""
        mock_client = _mock_copernicus_call()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_raster = MagicMock()
        mock_raster.read.return_value = np.array([[0.5, 0.6, 0.4]])
        mock_rasterio_open.return_value.__enter__.return_value = mock_raster

        # Small area (~25 km²) should get 256 on standard tier
        result = await service.get_ndvi(
            min_lat=42.0,
            max_lat=42.05,
            min_lon=-122.0,
            max_lon=-121.95,
            format="raster",
        )

        assert result["status"] == "success"
        # Should be one of the standard tier values, not 1024
        assert result["raster"]["width"] in (128, 256, 512)

    @patch("ember.services.copernicus.httpx.AsyncClient")
    async def test_truecolor_with_max_size(self, mock_client_class, service):
        """Truecolor with max_size=960 produces 960x960 output."""
        mock_client = _mock_copernicus_call()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.get_truecolor(
            min_lat=42.0,
            max_lat=42.5,
            min_lon=-122.0,
            max_lon=-121.5,
            format="png",
            max_size=960,
        )

        assert result["status"] == "success"
        assert result["raster"]["width"] == 960
        assert result["raster"]["height"] == 960

    @patch("ember.services.copernicus.httpx.AsyncClient")
    async def test_truecolor_without_max_size_uses_high_tier(self, mock_client_class, service):
        """Truecolor without max_size uses high resolution tier."""
        mock_client = _mock_copernicus_call()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.get_truecolor(
            min_lat=42.0,
            max_lat=42.05,
            min_lon=-122.0,
            max_lon=-121.95,
            format="png",
        )

        assert result["status"] == "success"
        # High tier values: 256, 512, 1024
        assert result["raster"]["width"] in (256, 512, 1024)


# ============================================================================
# max_size validation
# ============================================================================


class TestMaxSizeValidation:
    """Verify max_size validation at the service level."""

    @patch("ember.services.copernicus.httpx.AsyncClient")
    async def test_max_size_too_small(self, mock_client_class, service):
        """max_size < 64 returns error before any API call."""
        result = await service.get_ndvi(
            min_lat=42.0,
            max_lat=42.5,
            min_lon=-122.0,
            max_lon=-121.5,
            format="raster",
            max_size=32,
        )
        assert result["status"] == "error"
        assert "max_size" in result["message"]
        # Verify no external calls were made
        mock_client_class.assert_not_called()

    @patch("ember.services.copernicus.httpx.AsyncClient")
    async def test_max_size_too_large(self, mock_client_class, service):
        """max_size > 2048 returns error before any API call."""
        result = await service.get_ndvi(
            min_lat=42.0,
            max_lat=42.5,
            min_lon=-122.0,
            max_lon=-121.5,
            format="raster",
            max_size=4096,
        )
        assert result["status"] == "error"
        assert "max_size" in result["message"]
        # Verify no external calls were made
        mock_client_class.assert_not_called()


# ============================================================================
# Cache key isolation with max_size
# ============================================================================


class TestMaxSizeCacheIsolation:
    """Verify different max_size values produce different cache keys."""

    @patch("ember.services.copernicus.rasterio.open")
    @patch("ember.services.copernicus.httpx.AsyncClient")
    async def test_different_max_size_different_cache_entries(
        self, mock_client_class, mock_rasterio_open, service
    ):
        """Same bbox at different max_size values should not collide in cache."""
        mock_raster = MagicMock()
        mock_raster.read.return_value = np.array([[0.5, 0.6, 0.4]])
        mock_rasterio_open.return_value.__enter__.return_value = mock_raster

        # Need enough side_effects for two full calls
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=[
                # Call 1: token + process
                MagicMock(
                    json=MagicMock(return_value={"access_token": "t", "expires_in": 3600}),
                    raise_for_status=MagicMock(),
                ),
                MagicMock(content=b"data_512", raise_for_status=MagicMock()),
                # Call 2: process only (token cached)
                MagicMock(content=b"data_1024", raise_for_status=MagicMock()),
            ]
        )
        mock_client_class.return_value.__aenter__.return_value = mock_client

        params = dict(
            min_lat=42.0,
            max_lat=42.5,
            min_lon=-122.0,
            max_lon=-121.5,
            format="raster",
        )

        await service.get_ndvi(**params, max_size=512)
        await service.get_ndvi(**params, max_size=1024)

        # Should have 2 separate cache entries
        ndvi_keys = [k for k in _product_cache if k.startswith("ndvi:")]
        assert len(ndvi_keys) == 2


# ============================================================================
# Terrain max_size passthrough
# ============================================================================


class TestTerrainMaxSize:
    """Verify terrain router passes max_size to service."""

    @patch("ember.routers.terrain.get_terrain_service")
    async def test_terrain_max_size_passed_to_service(self, mock_get_service):
        """Terrain endpoint passes max_size to query_terrain_bbox_raster."""
        from fastapi.testclient import TestClient

        from ember.main import create_app

        mock_service = MagicMock()
        mock_service.available_layers = ["elevation", "slope"]
        mock_service.query_terrain_bbox_raster = AsyncMock(
            return_value={
                "status": "success",
                "layer": "elevation",
                "bbox": [-122.0, 42.0, -121.5, 42.5],
                "raster": {
                    "format": "geotiff",
                    "encoding": "base64",
                    "data": "abc123",
                    "width": 1024,
                    "height": 1024,
                },
                "stats": {"min": 100.0, "max": 2000.0, "mean": 500.0},
            }
        )
        mock_get_service.return_value = mock_service

        app = create_app()
        client = TestClient(app)

        response = client.get(
            "/api/v1/terrain",
            params={
                "min_lat": 42.0,
                "max_lat": 42.5,
                "min_lon": -122.0,
                "max_lon": -121.5,
                "layers": "elevation",
                "format": "raster",
                "max_size": 1024,
            },
        )

        assert response.status_code == 200
        mock_service.query_terrain_bbox_raster.assert_called_once()
        call_kwargs = mock_service.query_terrain_bbox_raster.call_args[1]
        assert call_kwargs["max_size"] == 1024

    @patch("ember.routers.terrain.get_terrain_service")
    async def test_terrain_without_max_size_uses_default(self, mock_get_service):
        """Terrain endpoint without max_size doesn't pass it (uses service default)."""
        from fastapi.testclient import TestClient

        from ember.main import create_app

        mock_service = MagicMock()
        mock_service.available_layers = ["elevation"]
        mock_service.query_terrain_bbox_raster = AsyncMock(
            return_value={
                "status": "success",
                "layer": "elevation",
                "bbox": [-122.0, 42.0, -121.5, 42.5],
                "raster": {
                    "format": "geotiff",
                    "encoding": "base64",
                    "data": "abc123",
                    "width": 512,
                    "height": 512,
                },
                "stats": {"min": 100.0, "max": 2000.0, "mean": 500.0},
            }
        )
        mock_get_service.return_value = mock_service

        app = create_app()
        client = TestClient(app)

        response = client.get(
            "/api/v1/terrain",
            params={
                "min_lat": 42.0,
                "max_lat": 42.5,
                "min_lon": -122.0,
                "max_lon": -121.5,
                "layers": "elevation",
                "format": "raster",
            },
        )

        assert response.status_code == 200
        call_kwargs = mock_service.query_terrain_bbox_raster.call_args[1]
        assert "max_size" not in call_kwargs
