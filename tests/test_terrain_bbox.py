#!/usr/bin/env python3
"""
Unit tests for terrain bbox raster queries.
"""

import os
import pytest

# Set environment variable BEFORE any imports
os.environ.setdefault("LANDFIRE_S3_PREFIX", "s3://landfire/")

from ember.services.terrain import get_terrain_service


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def terrain_service():
    """Provide terrain service instance for tests."""
    service = get_terrain_service()
    assert service is not None, "Terrain service should be available"
    return service


@pytest.fixture
def valid_bbox():
    """Standard test bbox (Los Angeles area)."""
    return {
        "min_lat": 34.0,
        "max_lat": 34.5,
        "min_lon": -118.5,
        "max_lon": -118.0,
    }


# =============================================================================
# Parameterized Validation Tests
# =============================================================================

class TestTerrainBboxValidation:
    """Input validation tests using parameterization."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("max_size,expected_msg", [
        (3000, "must be between 1 and 2048"),
        (0, "must be between 1 and 2048"),
        (-1, "must be between 1 and 2048"),
    ])
    async def test_invalid_max_size(self, terrain_service, valid_bbox, max_size, expected_msg):
        """max_size outside 1-2048 range should return error."""
        result = await terrain_service.query_terrain_bbox_raster(
            **valid_bbox,
            layer="fuel",
            max_size=max_size,
        )
        assert result["status"] == "error"
        assert expected_msg in result["message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bbox_override,expected_msg", [
        ({"min_lat": -100}, "Latitude values must be between -90 and 90"),
        ({"max_lat": 100}, "Latitude values must be between -90 and 90"),
        ({"min_lon": -200}, "Longitude values must be between -180 and 180"),
        ({"max_lon": 200}, "Longitude values must be between -180 and 180"),
    ])
    async def test_invalid_coordinates(self, terrain_service, valid_bbox, bbox_override, expected_msg):
        """Invalid coordinate values should return appropriate error."""
        bbox = {**valid_bbox, **bbox_override}
        result = await terrain_service.query_terrain_bbox_raster(
            **bbox,
            layer="fuel",
        )
        assert result["status"] == "error"
        assert expected_msg in result["message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bbox,expected_msg", [
        ({"min_lat": 30.0, "max_lat": 45.0, "min_lon": -120.0, "max_lon": -115.0},
         "Bbox too large"),  # 15 degrees lat
        ({"min_lat": 34.0, "max_lat": 35.0, "min_lon": -130.0, "max_lon": -115.0},
         "Bbox too large"),  # 15 degrees lon
    ])
    async def test_bbox_too_large(self, terrain_service, bbox, expected_msg):
        """Bbox > 10 degrees per dimension should return error."""
        result = await terrain_service.query_terrain_bbox_raster(
            **bbox,
            layer="fuel",
        )
        assert result["status"] == "error"
        assert expected_msg in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_layer(self, terrain_service, valid_bbox):
        """Unknown layer name should return error."""
        result = await terrain_service.query_terrain_bbox_raster(
            **valid_bbox,
            layer="nonexistent_layer",
        )
        assert result["status"] == "error"
        assert "not available" in result["message"]


# =============================================================================
# Success Case Tests
# =============================================================================

class TestTerrainBboxSuccess:
    """Tests for valid inputs (may fail at processing if S3 not configured)."""

    @pytest.mark.asyncio
    async def test_valid_input_passes_validation(self, terrain_service, valid_bbox):
        """Valid inputs should not return validation errors."""
        result = await terrain_service.query_terrain_bbox_raster(
            **valid_bbox,
            layer="fuel",
            max_size=512,
        )

        # If error, should NOT be a validation error
        if result["status"] == "error":
            validation_errors = [
                "max_size", "Latitude values", "Longitude values",
                "Bbox too large", "min values must be less than max"
            ]
            assert not any(msg in result.get("message", "") for msg in validation_errors), \
                f"Got unexpected validation error: {result['message']}"