#!/usr/bin/env python3
"""
Unit tests for terrain router bbox functionality.
Tests the extended get_terrain endpoint with bbox parameters.
"""

import os
import pytest
from fastapi.testclient import TestClient

# Set environment variable BEFORE any imports
os.environ.setdefault("LANDFIRE_S3_PREFIX", "s3://landfire/")

from ember.auth import verify_token
from ember.main import create_app


_DEV_USER = {"sub": "dev-user", "email": "dev@localhost", "auth_type": "dev"}


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def client():
    """Provide TestClient for the FastAPI app."""
    app = create_app()
    app.dependency_overrides[verify_token] = lambda: _DEV_USER
    return TestClient(app)


# =============================================================================
# Router Validation Tests
# =============================================================================

class TestTerrainRouterValidation:
    """Tests for router parameter validation."""

    def test_missing_parameters_returns_400(self, client):
        """Missing both point and bbox params should return 400."""
        response = client.get("/api/v1/terrain")
        assert response.status_code == 400
        assert "Must provide either" in response.json()["detail"]

    def test_partial_bbox_parameters_returns_400(self, client):
        """Partial bbox parameters should return helpful error message."""
        # Missing max_lon
        response = client.get("/api/v1/terrain?min_lat=34.0&max_lat=34.5&min_lon=-118.5")
        assert response.status_code == 400
        assert "Incomplete bbox" in response.json()["detail"]
        assert "max_lon" in response.json()["detail"]
        assert "All four bbox parameters required" in response.json()["detail"]

    def test_mixing_point_and_bbox_returns_400(self, client):
        """Mixing point and bbox params should return 400."""
        response = client.get("/api/v1/terrain?lat=34.0&lon=-118.0&min_lat=34.0&max_lat=34.5&min_lon=-118.5&max_lon=-118.0")
        assert response.status_code == 400
        assert "Cannot mix point and bbox parameters" in response.json()["detail"]

    def test_bbox_raster_without_layer_returns_400(self, client):
        """Bbox raster format without layer should return 400."""
        response = client.get("/api/v1/terrain?min_lat=34.0&max_lat=34.5&min_lon=-118.5&max_lon=-118.0&format=raster")
        assert response.status_code == 400
        assert "Raster format requires exactly one layer" in response.json()["detail"]

    def test_bbox_raster_with_multiple_layers_returns_400(self, client):
        """Bbox raster format with multiple layers should return 400."""
        response = client.get("/api/v1/terrain?min_lat=34.0&max_lat=34.5&min_lon=-118.5&max_lon=-118.0&layers=fuel,elevation&format=raster")
        assert response.status_code == 400
        assert "Raster format supports exactly one layer" in response.json()["detail"]

    def test_bbox_raster_with_invalid_layer_returns_400(self, client):
        """Bbox raster format with invalid layer should return 400."""
        response = client.get("/api/v1/terrain?min_lat=34.0&max_lat=34.5&min_lon=-118.5&max_lon=-118.0&layers=invalid_layer&format=raster")
        assert response.status_code == 400
        assert "Unknown layer: invalid_layer" in response.json()["detail"]

    def test_bbox_json_returns_400(self, client):
        """Bbox JSON format should return 400 (not yet implemented)."""
        response = client.get("/api/v1/terrain?min_lat=34.0&max_lat=34.5&min_lon=-118.5&max_lon=-118.0&format=json")
        assert response.status_code == 400
        assert "Bbox queries currently only support format=raster" in response.json()["detail"]


# =============================================================================
# Router Success Tests
# =============================================================================

class TestTerrainRouterSuccess:
    """Tests for successful router responses."""

    def test_point_query_still_works(self, client):
        """Original point query should still work (backward compatibility)."""
        response = client.get("/api/v1/terrain?lat=34.0&lon=-118.0")
        # Should return 200 or 502 (service error) but not 400 (validation error)
        assert response.status_code in [200, 502]
        # If it succeeds, check structure. If service error, that's also fine (S3 access issues)
        if response.status_code == 200:
            data = response.json()
            # Point queries return layer data with this structure
            assert "fuel" in data  # The actual layer data
            assert "latitude" in data
            assert "longitude" in data
            assert "layers_queried" in data
        # 502 means router worked but service failed (expected in test env)

    def test_bbox_raster_query_structure(self, client):
        """Bbox raster query should return proper structure (though may fail at service level)."""
        response = client.get("/api/v1/terrain?min_lat=34.0&max_lat=34.5&min_lon=-118.5&max_lon=-118.0&layers=fuel&format=raster")
        # Should return 200 (success) or 502 (service error) but not 400 (validation error)
        assert response.status_code in [200, 502]
        # The important thing is that it's NOT a 400 validation error
        # 502 means router worked but service failed (expected in test env without S3 access)
        # We can't test the success case without real S3 credentials

    def test_layers_endpoint_still_works(self, client):
        """Layers endpoint should still work."""
        response = client.get("/api/v1/terrain/layers")
        assert response.status_code == 200
        data = response.json()
        assert "available" in data
        assert "layers" in data
