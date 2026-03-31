"""Router-level tests for GET /api/v1/weather/air-quality."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from ember.auth import verify_token
from ember.main import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DEV_USER = {"sub": "dev-user", "email": "dev@localhost", "auth_type": "dev"}

_AQI_RESULT = {
    "status": "success",
    "latitude": 46.89,
    "longitude": -117.56,
    "aqi": 42,
    "category": "Good",
    "dominant_pollutant": "PM2.5",
    "pollutants": {
        "pm25": {"aqi": 42, "concentration": 10.2, "unit": "µg/m³"},
        "pm10": {"aqi": 28, "concentration": 32.0, "unit": "µg/m³"},
        "ozone": {"aqi": 35, "concentration": 0.042, "unit": "ppm"},
    },
    "source": "EPA AirNow",
    "retrieved_at": "2026-03-31T14:00:00Z",
}

_NO_DATA_RESULT = {
    "status": "no_data",
    "message": "No monitoring stations found within 25.0 miles",
    "latitude": 46.89,
    "longitude": -117.56,
    "source": "EPA AirNow",
    "retrieved_at": "2026-03-31T14:00:00Z",
}


@pytest.fixture
def client():
    app = create_app()
    app.dependency_overrides[verify_token] = lambda: _DEV_USER
    return TestClient(app)


@pytest.fixture
def mock_aqi():
    with patch(
        "ember.routers.weather.airquality_service.get_air_quality",
        new_callable=AsyncMock,
        return_value=_AQI_RESULT.copy(),
    ) as mock:
        yield mock


@pytest.fixture
def mock_aqi_no_data():
    with patch(
        "ember.routers.weather.airquality_service.get_air_quality",
        new_callable=AsyncMock,
        return_value=_NO_DATA_RESULT.copy(),
    ) as mock:
        yield mock


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestAirQualityValidation:
    def test_missing_lat_returns_422(self, client):
        response = client.get("/api/v1/weather/air-quality?lon=-117.56")
        assert response.status_code == 422

    def test_missing_lon_returns_422(self, client):
        response = client.get("/api/v1/weather/air-quality?lat=46.89")
        assert response.status_code == 422

    def test_lat_out_of_range_returns_422(self, client):
        response = client.get("/api/v1/weather/air-quality?lat=91&lon=-117")
        assert response.status_code == 422

    def test_lon_out_of_range_returns_422(self, client):
        response = client.get("/api/v1/weather/air-quality?lat=46&lon=181")
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Success tests
# ---------------------------------------------------------------------------


class TestAirQualitySuccess:
    def test_returns_200_with_aqi(self, client, mock_aqi):
        response = client.get("/api/v1/weather/air-quality?lat=46.89&lon=-117.56")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["aqi"] == 42
        assert data["category"] == "Good"
        assert data["dominant_pollutant"] == "PM2.5"

    def test_pollutants_structure(self, client, mock_aqi):
        data = client.get("/api/v1/weather/air-quality?lat=46.89&lon=-117.56").json()
        assert "pm25" in data["pollutants"]
        assert data["pollutants"]["pm25"]["aqi"] == 42

    def test_no_data_returns_200(self, client, mock_aqi_no_data):
        response = client.get("/api/v1/weather/air-quality?lat=46.89&lon=-117.56")
        assert response.status_code == 200
        assert response.json()["status"] == "no_data"

    def test_service_called_with_correct_params(self, client, mock_aqi):
        client.get("/api/v1/weather/air-quality?lat=46.89&lon=-117.56")
        mock_aqi.assert_called_once_with(46.89, -117.56)

    def test_response_includes_source_and_retrieved_at(self, client, mock_aqi):
        data = client.get("/api/v1/weather/air-quality?lat=46.89&lon=-117.56").json()
        assert data["source"] == "EPA AirNow"
        assert "retrieved_at" in data


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestAirQualityErrors:
    def test_missing_api_key_returns_400(self, client):
        with patch(
            "ember.routers.weather.airquality_service.get_air_quality",
            new_callable=AsyncMock,
            side_effect=ValueError("AIRNOW_API_KEY not configured"),
        ):
            response = client.get("/api/v1/weather/air-quality?lat=46.89&lon=-117.56")
        assert response.status_code == 400
        assert "AIRNOW_API_KEY" in response.json()["detail"]

    def test_service_exception_returns_502(self, client):
        with patch(
            "ember.routers.weather.airquality_service.get_air_quality",
            new_callable=AsyncMock,
            side_effect=RuntimeError("EPA API timeout"),
        ):
            response = client.get("/api/v1/weather/air-quality?lat=46.89&lon=-117.56")
        assert response.status_code == 502
        assert "AirNow API error" in response.json()["detail"]
