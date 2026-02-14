#!/usr/bin/env python3
"""
Test suite for vegetation endpoints using pytest with parametrization and fixtures.
"""

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

# Set environment variables before importing ember modules
os.environ["COPERNICUS_CLIENT_ID"] = ""
os.environ["COPERNICUS_CLIENT_SECRET"] = ""

from ember.services.copernicus import CopernicusService


@pytest.fixture
def copernicus_service_fixture():
    """Fixture providing a Copernicus service instance with test configuration."""
    service = CopernicusService()
    # Ensure credentials are empty for testing
    service.client_id = ""
    service.client_secret = ""
    return service


@pytest.fixture
def copernicus_service_with_credentials():
    """Fixture providing a Copernicus service with dummy credentials for validation testing."""
    service = CopernicusService()
    service.client_id = "dummy"
    service.client_secret = "dummy"
    return service


@pytest.mark.parametrize(
    "lat,lon,size_km,expected_status",
    [
        (38.85, -120.89, 5.0, "not_configured"),
        (34.05, -118.24, 10.0, "not_configured"),
        (40.71, -74.00, 2.5, "not_configured"),
    ],
)
async def test_ndvi_stats_various_locations(
    lat, lon, size_km, expected_status, copernicus_service_fixture
):
    """Test NDVI stats endpoint with various locations."""
    result = await copernicus_service_fixture.get_ndvi(
        lat=lat, lon=lon, size_km=size_km, format="stats"
    )
    assert result["status"] == expected_status
    assert "message" in result


@pytest.mark.parametrize("format", ["stats", "raster"])
async def test_ndvi_both_formats(format, copernicus_service_fixture):
    """Test NDVI endpoint with both response formats."""
    result = await copernicus_service_fixture.get_ndvi(
        lat=38.85, lon=-120.89, size_km=5.0, format=format
    )
    assert result["status"] == "not_configured"
    if format == "stats":
        assert "ndvi" not in result  # Stats should not be present when not configured
    elif format == "raster":
        assert (
            "raster" not in result
        )  # Raster should not be present when not configured


@pytest.mark.parametrize(
    "min_lat,max_lat,min_lon,max_lon",
    [
        (38.5, 39.2, -121.5, -120.3),
        (34.0, 34.5, -118.5, -118.0),
    ],
)
async def test_ndvi_bbox_mode(
    min_lat, max_lat, min_lon, max_lon, copernicus_service_fixture
):
    """Test NDVI endpoint with bbox parameters."""
    result = await copernicus_service_fixture.get_ndvi(
        min_lat=min_lat,
        max_lat=max_lat,
        min_lon=min_lon,
        max_lon=max_lon,
        format="stats",
    )
    assert result["status"] == "not_configured"


@pytest.mark.parametrize(
    "lat,lon,size_km",
    [
        (38.85, -120.89, 5.0),
        (34.05, -118.24, 10.0),
    ],
)
async def test_ndmi_endpoint(lat, lon, size_km, copernicus_service_fixture):
    """Test NDMI endpoint with various parameters."""
    result = await copernicus_service_fixture.get_ndmi(
        lat=lat, lon=lon, size_km=size_km, format="stats"
    )
    assert result["status"] == "not_configured"


@pytest.mark.parametrize(
    "invalid_params,error_message",
    [
        ({"lat": 38.85}, "Both lat and lon must be provided together"),
        ({"lon": -120.89}, "Both lat and lon must be provided together"),
        ({"min_lat": 38.5}, "All four bbox parameters"),
        ({"max_lat": 39.2}, "All four bbox parameters"),
        ({"min_lon": -121.5}, "All four bbox parameters"),
        ({"max_lon": -120.3}, "All four bbox parameters"),
    ],
)
async def test_parameter_validation_errors(
    invalid_params, error_message, copernicus_service_with_credentials
):
    """Test parameter validation error handling."""
    result = await copernicus_service_with_credentials.get_ndvi(
        format="stats", **invalid_params
    )
    assert result["status"] == "error"
    assert error_message in result["message"]


@pytest.mark.parametrize(
    "size_km,expected_error",
    [
        (0, "size_km must be between 1 and 100"),
        (101, "size_km must be between 1 and 100"),
        (-5, "size_km must be between 1 and 100"),
    ],
)
async def test_size_km_validation(
    size_km, expected_error, copernicus_service_with_credentials
):
    """Test size_km parameter validation."""
    result = await copernicus_service_with_credentials.get_ndvi(
        lat=38.85, lon=-120.89, size_km=size_km, format="stats"
    )
    assert result["status"] == "error"
    assert expected_error in result["message"]


@pytest.mark.parametrize(
    "service_method,params",
    [
        ("get_ndvi", {"lat": 38.85, "lon": -120.89, "size_km": 5.0}),
        ("get_ndmi", {"lat": 38.85, "lon": -120.89, "size_km": 5.0}),
    ],
)
async def test_cache_key_generation(service_method, params, copernicus_service_fixture):
    """Test that cache keys are generated without errors."""
    method = getattr(copernicus_service_fixture, service_method)

    # First call
    result1 = await method(format="stats", **params)
    assert "status" in result1

    # Second call with same parameters should work (cache logic doesn't fail)
    result2 = await method(format="stats", **params)
    assert "status" in result2


@pytest.mark.parametrize(
    "invalid_bbox,error_detail",
    [
        ({"min_lat": 40, "max_lat": 30}, "min_lat must be less than max_lat"),
        ({"min_lon": -120, "max_lon": -125}, "min_lon must be less than max_lon"),
    ],
)
@patch("ember.services.copernicus.CopernicusService._call_process_api")
async def test_bbox_validation_errors(
    mock_process_api, invalid_bbox, error_detail, copernicus_service_with_credentials
):
    """Test bbox coordinate validation - should fail before API call."""
    result = await copernicus_service_with_credentials.get_ndvi(
        format="stats", **invalid_bbox
    )
    
    # Verify no API call was made
    mock_process_api.assert_not_called()
    
    assert result["status"] == "error"
    assert error_detail in result["message"]


@pytest.mark.parametrize("invalid_format", ["invalid", "json", "image", ""])
@patch("ember.services.copernicus.CopernicusService._call_process_api")
async def test_format_validation(
    mock_process_api, invalid_format, copernicus_service_with_credentials
):
    """Test format parameter validation - should fail before API call."""
    result = await copernicus_service_with_credentials.get_ndvi(
        lat=38.85, lon=-120.89, size_km=5.0, format=invalid_format
    )
    
    # Verify no API call was made
    mock_process_api.assert_not_called()
    
    assert result["status"] == "error"
    assert "Invalid format" in result["message"]


@pytest.mark.parametrize(
    "invalid_date,date_param",
    [
        (12345, "start_date"),
        (None, "start_date"),  # None should be handled gracefully
        ("not-a-date", "start_date"),
    ],
)
@patch("ember.services.copernicus.CopernicusService._call_process_api")
async def test_date_validation(
    mock_process_api, invalid_date, date_param, copernicus_service_with_credentials
):
    """Test date parameter validation - should fail before API call for invalid types."""
    params = {"lat": 38.85, "lon": -120.89, "size_km": 5.0, date_param: invalid_date}

    result = await copernicus_service_with_credentials.get_ndvi(
        format="stats", **params
    )

    # None should be fine (defaults will be used)
    if invalid_date is None:
        # With credentials set, it will try to make API call, but that's expected behavior
        # The important thing is that None doesn't cause validation errors
        assert result["status"] in ["error", "not_configured"]
    else:
        # Invalid types should be caught by validation
        if isinstance(invalid_date, int):
            # Verify no API call was made for invalid types
            mock_process_api.assert_not_called()
            assert result["status"] == "error"
            assert "must be a string" in result["message"]
        else:
            # String format validation happens at service level
            # For string dates, we expect either error or not_configured
            assert result["status"] in ["error", "not_configured"]


def test_service_initialization():
    """Test that service initializes correctly."""
    service = CopernicusService()
    assert hasattr(service, "client_id")
    assert hasattr(service, "client_secret")
    assert hasattr(service, "base_url")
    assert hasattr(service, "process_endpoint")
    assert hasattr(service, "token_url")


@pytest.mark.parametrize(
    "ndvi_value,expected_status",
    [
        (0.05, "Bare/Barren"),
        (0.15, "Sparse Vegetation"),
        (0.30, "Moderate Vegetation"),
        (0.50, "Healthy Vegetation"),
        (0.70, "Dense Vegetation"),
    ],
)
def test_ndvi_classification(ndvi_value, expected_status):
    """Test NDVI classification logic."""
    service = CopernicusService()
    assert service._ndvi_to_status(ndvi_value) == expected_status


@pytest.mark.parametrize(
    "ndmi_value,expected_moisture,expected_risk",
    [
        (-0.3, "Very Dry", "High"),
        (-0.1, "Dry", "Moderate"),  # -0.1 is not < -0.1, so returns "Moderate"
        (0.0, "Moderate", "Moderate"),
        (0.15, "Moderate", "Low"),  # 0.15 is not < 0.1, so returns "Low"
        (0.25, "Moist", "Low"),
        (0.45, "Saturated", "Low"),
    ],
)
def test_ndmi_classification(ndmi_value, expected_moisture, expected_risk):
    """Test NDMI classification logic."""
    service = CopernicusService()
    assert service._ndmi_to_moisture_status(ndmi_value) == expected_moisture
    assert service._ndmi_to_fire_risk(ndmi_value) == expected_risk


# Integration-style test (would require real credentials in CI)
@pytest.mark.skip(reason="Requires real Copernicus credentials")
async def test_real_api_integration():
    """Integration test with real Copernicus API (skipped by default)."""
    # This would be enabled in CI with proper credentials
    service = CopernicusService()
    service.client_id = os.getenv("REAL_COPERNICUS_CLIENT_ID", "")
    service.client_secret = os.getenv("REAL_COPERNICUS_CLIENT_SECRET", "")

    if not service.client_id or not service.client_secret:
        pytest.skip("Real credentials not available")

    result = await service.get_ndvi(lat=38.85, lon=-120.89, size_km=5.0, format="stats")

    assert result["status"] == "success"
    assert "ndvi" in result
    assert "mean" in result["ndvi"]


if __name__ == "__main__":
    # Allow running tests directly
    pytest.main([__file__, "-v"])
