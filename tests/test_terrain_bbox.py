#!/usr/bin/env python3
"""
Simple test suite for terrain bbox raster queries that works around import issues.
"""

import asyncio
import os
import pytest

# Set environment variable BEFORE any imports
os.environ["LANDFIRE_S3_PREFIX"] = "s3://landfire/"

from ember.services.terrain import get_terrain_service

# Enable async test support
pytest_plugins = ("pytest_asyncio",)


# Test coordinates (Los Angeles area)
TEST_BBOX = {
    "min_lat": 34.0,
    "max_lat": 34.5,
    "min_lon": -118.5,
    "max_lon": -118.0,
}


class TestTerrainBboxValidation:
    """Unit tests for input validation in terrain bbox queries."""

    @pytest.mark.asyncio
    async def test_max_size_too_large_returns_error(self):
        """max_size > 2048 should return error."""
        service = get_terrain_service()
        assert service is not None, "Terrain service should be available"

        result = await service.query_terrain_bbox_raster(
            **TEST_BBOX,
            layer="fuel",  # Use fuel since it's available in test config
            max_size=3000,  # Too large
        )

        assert result["status"] == "error"
        assert "max_size" in result["message"]
        assert "must be between 1 and 2048" in result["message"]

    @pytest.mark.asyncio
    async def test_max_size_zero_returns_error(self):
        """max_size <= 0 should return error."""
        service = get_terrain_service()
        
        result = await service.query_terrain_bbox_raster(
            **TEST_BBOX,
            layer="fuel",
            max_size=0,  # Invalid
        )

        assert result["status"] == "error"
        assert "max_size" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_latitude_returns_error(self):
        """Latitude outside -90 to 90 should return error."""
        service = get_terrain_service()
        
        invalid_bbox = TEST_BBOX.copy()
        invalid_bbox["min_lat"] = -100  # Invalid latitude

        result = await service.query_terrain_bbox_raster(
            **invalid_bbox,
            layer="fuel",
        )

        assert result["status"] == "error"
        assert "Latitude values must be between -90 and 90" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_longitude_returns_error(self):
        """Longitude outside -180 to 180 should return error."""
        service = get_terrain_service()
        
        invalid_bbox = TEST_BBOX.copy()
        invalid_bbox["min_lon"] = -200  # Invalid longitude

        result = await service.query_terrain_bbox_raster(
            **invalid_bbox,
            layer="fuel",
        )

        assert result["status"] == "error"
        assert "Longitude values must be between -180 and 180" in result["message"]

    @pytest.mark.asyncio
    async def test_bbox_too_large_returns_error(self):
        """Bbox > 10 degrees should return error."""
        service = get_terrain_service()
        
        large_bbox = {
            "min_lat": 30.0,
            "max_lat": 45.0,  # 15 degrees - too large
            "min_lon": -120.0,
            "max_lon": -110.0,
        }

        result = await service.query_terrain_bbox_raster(
            **large_bbox,
            layer="fuel",
        )

        assert result["status"] == "error"
        assert "Bbox too large (max 10 degrees per dimension)" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_layer_returns_error(self):
        """Unknown layer name should return error."""
        service = get_terrain_service()
        
        result = await service.query_terrain_bbox_raster(
            **TEST_BBOX,
            layer="nonexistent_layer",
        )

        assert result["status"] == "error"
        assert "Layer 'nonexistent_layer' not available" in result["message"]

    @pytest.mark.asyncio
    async def test_valid_bbox_with_fuel_layer(self):
        """Valid bbox with fuel layer should pass validation (though may fail at processing)."""
        service = get_terrain_service()
        
        result = await service.query_terrain_bbox_raster(
            **TEST_BBOX,
            layer="fuel",
            max_size=512,
        )

        # Should not return validation errors
        if result["status"] == "error":
            # If it's an error, it should not be a validation error
            assert "max_size" not in result.get("message", "")
            assert "Latitude values must be between" not in result.get("message", "")
            assert "Longitude values must be between" not in result.get("message", "")
            assert "Bbox too large" not in result.get("message", "")
            assert "Invalid bbox: min values must be less than max values" not in result.get("message", "")