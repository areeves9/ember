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


# =============================================================================
# LANDFIRE Coverage Validation Tests
# =============================================================================

class TestBboxOverlapHelper:
    """Unit tests for the bbox overlap primitive."""

    def test_overlapping_bboxes(self):
        from ember.routers.terrain import _bboxes_overlap
        a = (-120.0, 30.0, -110.0, 40.0)
        b = (-115.0, 35.0, -105.0, 45.0)
        assert _bboxes_overlap(a, b) is True

    def test_disjoint_bboxes(self):
        from ember.routers.terrain import _bboxes_overlap
        a = (-120.0, 30.0, -110.0, 40.0)
        b = (0.0, 30.0, 10.0, 40.0)
        assert _bboxes_overlap(a, b) is False

    def test_touching_edges_do_not_overlap(self):
        """Strict inequality — sharing an edge is not an overlap."""
        from ember.routers.terrain import _bboxes_overlap
        a = (-120.0, 30.0, -110.0, 40.0)
        b = (-110.0, 30.0, -100.0, 40.0)  # shares east edge of a
        assert _bboxes_overlap(a, b) is False

    def test_one_contains_other(self):
        from ember.routers.terrain import _bboxes_overlap
        outer = (-130.0, 20.0, -100.0, 50.0)
        inner = (-120.0, 30.0, -110.0, 40.0)
        assert _bboxes_overlap(outer, inner) is True
        assert _bboxes_overlap(inner, outer) is True


class TestLandfireCoverage:
    """Unit tests for LANDFIRE coverage detection."""

    def test_conus_bbox_intersects(self):
        from ember.routers.terrain import _bbox_intersects_landfire
        # Los Angeles area
        assert _bbox_intersects_landfire((-118.5, 34.0, -118.0, 34.5)) is True

    def test_alaska_bbox_intersects(self):
        from ember.routers.terrain import _bbox_intersects_landfire
        # Fairbanks area
        assert _bbox_intersects_landfire((-147.5, 64.0, -147.0, 64.5)) is True

    def test_hawaii_bbox_intersects(self):
        from ember.routers.terrain import _bbox_intersects_landfire
        # Big Island
        assert _bbox_intersects_landfire((-156.0, 19.0, -155.5, 19.5)) is True

    def test_europe_bbox_does_not_intersect(self):
        from ember.routers.terrain import _bbox_intersects_landfire
        # Paris
        assert _bbox_intersects_landfire((2.0, 48.5, 2.5, 49.0)) is False

    def test_atlantic_bbox_does_not_intersect(self):
        from ember.routers.terrain import _bbox_intersects_landfire
        assert _bbox_intersects_landfire((-40.0, 30.0, -30.0, 40.0)) is False

    def test_bbox_spanning_pacific_to_conus_intersects(self):
        """A bbox that partially overlaps CONUS should be allowed."""
        from ember.routers.terrain import _bbox_intersects_landfire
        # West edge in Pacific, east edge in California
        assert _bbox_intersects_landfire((-140.0, 35.0, -118.0, 40.0)) is True


class TestRouterCoverageRejection:
    """Integration tests: router returns 400 for bboxes outside LANDFIRE coverage."""

    def test_bbox_in_europe_returns_400(self, client):
        response = client.get(
            "/api/v1/terrain?min_lat=48.5&max_lat=49.0&min_lon=2.0&max_lon=2.5&layers=fuel&format=raster"
        )
        assert response.status_code == 400
        assert "outside LANDFIRE coverage" in response.json()["detail"]

    def test_bbox_in_atlantic_returns_400(self, client):
        response = client.get(
            "/api/v1/terrain?min_lat=30.0&max_lat=40.0&min_lon=-40.0&max_lon=-30.0&layers=fuel&format=raster"
        )
        assert response.status_code == 400
        assert "outside LANDFIRE coverage" in response.json()["detail"]

    def test_bbox_in_europe_json_format_also_returns_coverage_error(self, client):
        """Coverage check should fire before format-specific handling."""
        response = client.get(
            "/api/v1/terrain?min_lat=48.5&max_lat=49.0&min_lon=2.0&max_lon=2.5&format=json"
        )
        assert response.status_code == 400
        assert "outside LANDFIRE coverage" in response.json()["detail"]

    def test_bbox_in_conus_not_blocked_by_coverage_check(self, client):
        """CONUS bbox should pass coverage validation (may still 502 at service layer)."""
        response = client.get(
            "/api/v1/terrain?min_lat=34.0&max_lat=34.5&min_lon=-118.5&max_lon=-118.0&layers=fuel&format=raster"
        )
        # 200 = success, 502 = service error (no S3 in tests).
        # 400 would mean coverage check rejected it — that's the bug we're guarding against.
        assert response.status_code in [200, 502]
