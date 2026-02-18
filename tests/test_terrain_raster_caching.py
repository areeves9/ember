#!/usr/bin/env python3
"""
Simplified unit tests for terrain raster caching functionality.
"""

import asyncio
import os
import time
from unittest.mock import patch

import pytest

# Set environment variable BEFORE any imports
os.environ.setdefault("LANDFIRE_S3_PREFIX", "s3://landfire/")

from ember.services.terrain import get_terrain_service


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


class TestTerrainRasterCaching:
    """Tests for raster caching functionality."""

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_result(self, terrain_service, valid_bbox):
        """Second identical request should return cached result."""
        # Clear cache first
        from ember.services.terrain import _raster_cache
        _raster_cache.clear()
        
        with patch.object(terrain_service, '_read_bbox_raster') as mock_read:
            # Setup mock to return a success result
            mock_read.return_value = {
                "status": "success",
                "layer": "fuel",
                "bbox": [-118.5, 34.0, -118.0, 34.5],
                "raster": {
                    "format": "geotiff",
                    "encoding": "base64",
                    "data": "base64_encoded_data",
                    "width": 512,
                    "height": 512,
                },
                "stats": {"min": 1, "max": 10, "mean": 5},
            }

            # First request (cache miss)
            result1 = await terrain_service.query_terrain_bbox_raster(
                **valid_bbox,
                layer="fuel",
                max_size=512,
            )
            
            # Second request (cache hit)
            result2 = await terrain_service.query_terrain_bbox_raster(
                **valid_bbox,
                layer="fuel",
                max_size=512,
            )
            
            # Both should return success
            assert result1["status"] == "success"
            assert result2["status"] == "success"
            
            # Second request should not call the mock (cached)
            assert mock_read.call_count == 1, "Cache should prevent second call"
            
            # Results should be identical
            assert result1 == result2

    @pytest.mark.asyncio
    async def test_cache_miss_calls_service_method(self, terrain_service, valid_bbox):
        """First request should call the service method."""
        # Clear cache first
        from ember.services.terrain import _raster_cache
        _raster_cache.clear()
        
        with patch.object(terrain_service, '_read_bbox_raster') as mock_read:
            mock_read.return_value = {
                "status": "success",
                "layer": "fuel",
                "bbox": [-118.5, 34.0, -118.0, 34.5],
                "raster": {
                    "format": "geotiff",
                    "encoding": "base64",
                    "data": "base64_encoded_data",
                    "width": 512,
                    "height": 512,
                },
                "stats": {"min": 1, "max": 10, "mean": 5},
            }

            result = await terrain_service.query_terrain_bbox_raster(
                **valid_bbox,
                layer="fuel",
                max_size=512,
            )
            
            assert result["status"] == "success"
            assert mock_read.call_count == 1, "First request should call service method"

    @pytest.mark.asyncio
    async def test_error_results_not_cached(self, terrain_service, valid_bbox):
        """Error results should not be cached."""
        # Clear cache first
        from ember.services.terrain import _raster_cache
        _raster_cache.clear()
        
        with patch.object(terrain_service, '_read_bbox_raster') as mock_read:
            # Mock returns error result
            mock_read.return_value = {
                "status": "error",
                "message": "Some error",
            }

            # First request
            result1 = await terrain_service.query_terrain_bbox_raster(
                **valid_bbox,
                layer="fuel",
                max_size=512,
            )
            
            # Second request
            result2 = await terrain_service.query_terrain_bbox_raster(
                **valid_bbox,
                layer="fuel",
                max_size=512,
            )
            
            # Both should return error
            assert result1["status"] == "error"
            assert result2["status"] == "error"
            
            # Both should call the service method (not cached)
            assert mock_read.call_count == 2, "Error results should not be cached"

    @pytest.mark.asyncio
    async def test_cache_key_rounding(self):
        """Test that cache key rounding works correctly."""
        from ember.services.terrain import _raster_cache_key
        
        # Exact same coordinates should produce same key
        key1 = _raster_cache_key("fuel", 34.0, 34.5, -118.5, -118.0, 512)
        key2 = _raster_cache_key("fuel", 34.0, 34.5, -118.5, -118.0, 512)
        assert key1 == key2
        
        # Coordinates that round to the same value should match
        key3 = _raster_cache_key("fuel", 34.0001, 34.4999, -118.500, -118.000, 512)
        assert key1 == key3, "Coordinates within rounding tolerance should match"
        
        # Different max_size should produce different key
        key4 = _raster_cache_key("fuel", 34.0, 34.5, -118.5, -118.0, 256)
        assert key1 != key4
        
        # Different layer should produce different key
        key5 = _raster_cache_key("elevation", 34.0, 34.5, -118.5, -118.0, 512)
        assert key1 != key5

    @pytest.mark.asyncio
    async def test_cache_size_management(self, terrain_service, valid_bbox):
        """Test that cache size is managed properly."""
        from ember.services.terrain import _raster_cache, _RASTER_CACHE_MAX_SIZE
        
        with patch.object(terrain_service, '_read_bbox_raster') as mock_read:
            mock_read.return_value = {
                "status": "success",
                "layer": "fuel",
                "bbox": [-118.5, 34.0, -118.0, 34.5],
                "raster": {
                    "format": "geotiff",
                    "encoding": "base64",
                    "data": "base64_encoded_data",
                    "width": 512,
                    "height": 512,
                },
                "stats": {"min": 1, "max": 10, "mean": 5},
            }

            # Fill cache to max size
            for i in range(_RASTER_CACHE_MAX_SIZE + 10):
                await terrain_service.query_terrain_bbox_raster(
                    min_lat=34.0 + i * 0.1,
                    max_lat=34.5 + i * 0.1,
                    min_lon=-118.5 + i * 0.1,
                    max_lon=-118.0 + i * 0.1,
                    layer="fuel",
                    max_size=512,
                )
            
            # Cache should not exceed max size
            assert len(_raster_cache) <= _RASTER_CACHE_MAX_SIZE
