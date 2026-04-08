"""Tests for anomaly factor baseline enrichment in FIRMS clustering.

Covers:
- Baseline computation from archive detections
- Novel events (no thermal history)
- Anomaly factor calculation
- Batch archive query caching
- Graceful degradation when FIRMS archive unavailable
- Haversine distance filtering
- GeoJSON properties include anomaly fields
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("FIRMS_MAP_KEY", "test_key")

from ember.services.firms import FirmsService, _baseline_cache, _fires_cache

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def clear_caches():
    """Clear all FIRMS caches before and after each test."""
    _fires_cache.clear()
    _baseline_cache.clear()
    yield
    _fires_cache.clear()
    _baseline_cache.clear()


@pytest.fixture
def service():
    """FirmsService with test API key."""
    svc = FirmsService()
    svc.api_key = "test_key"
    return svc


def _make_csv(rows: list[dict]) -> str:
    """Build FIRMS-style CSV from list of dicts."""
    if not rows:
        return "latitude,longitude,bright_ti4,frp,confidence,acq_date,acq_time,satellite,daynight\n"
    headers = list(rows[0].keys())
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(str(row[h]) for h in headers))
    return "\n".join(lines)


def _mock_firms_responses(current_csv: str, archive_csvs: list[str]):
    """Create mock httpx client that returns current + archive CSVs in sequence."""
    responses = []
    for csv_text in [current_csv] + archive_csvs:
        resp = MagicMock()
        resp.text = csv_text
        resp.raise_for_status = MagicMock()
        responses.append(resp)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=responses)
    return mock_client


# ============================================================================
# Haversine
# ============================================================================


class TestHaversine:
    """Test haversine distance calculation."""

    def test_same_point_is_zero(self, service):
        assert service._haversine_km(40.0, -100.0, 40.0, -100.0) == 0.0

    def test_known_distance(self, service):
        # New York to Los Angeles ~3944 km
        dist = service._haversine_km(40.7128, -74.006, 34.0522, -118.2437)
        assert 3900 < dist < 4000

    def test_short_distance(self, service):
        # ~1km apart at mid-latitudes
        dist = service._haversine_km(40.0, -100.0, 40.009, -100.0)
        assert 0.9 < dist < 1.1


# ============================================================================
# Baseline enrichment
# ============================================================================


class TestEnrichBaselines:
    """Test anomaly factor computation from archive detections."""

    @patch("ember.services.firms.httpx.AsyncClient")
    async def test_anomaly_factor_computed(self, mock_client_class, service):
        """Clusters get anomaly_factor from archive baseline."""
        # Current detections CSV
        current_csv = _make_csv(
            [
                {
                    "latitude": "35.5",
                    "longitude": "-102.3",
                    "bright_ti4": "350",
                    "frp": "500",
                    "confidence": "nominal",
                    "acq_date": "2026-04-06",
                    "acq_time": "1420",
                    "satellite": "N",
                    "daynight": "D",
                },
            ]
        )

        # Archive CSVs — baseline FRP of ~50 MW
        archive_csv = _make_csv(
            [
                {
                    "latitude": "35.5",
                    "longitude": "-102.3",
                    "bright_ti4": "300",
                    "frp": "50",
                    "confidence": "nominal",
                    "acq_date": "2026-03-28",
                    "acq_time": "1400",
                    "satellite": "N",
                    "daynight": "D",
                },
                {
                    "latitude": "35.51",
                    "longitude": "-102.31",
                    "bright_ti4": "300",
                    "frp": "50",
                    "confidence": "nominal",
                    "acq_date": "2026-03-29",
                    "acq_time": "1400",
                    "satellite": "N",
                    "daynight": "D",
                },
            ]
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[
                # Current fires request
                MagicMock(text=current_csv, raise_for_status=MagicMock()),
                # Archive window 1
                MagicMock(text=archive_csv, raise_for_status=MagicMock()),
                # Archive window 2
                MagicMock(text=archive_csv, raise_for_status=MagicMock()),
            ]
        )
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.get_fires(min_lat=35.0, max_lat=36.0, min_lon=-103.0, max_lon=-101.0)

        # Should have one cluster with anomaly enrichment
        assert result["cluster_count"] >= 1
        features = result["geojson"]["features"]
        marker = next(f for f in features if f["properties"]["layer"] == "marker")
        props = marker["properties"]

        assert "anomaly_factor" in props
        assert "baseline_frp_mw" in props
        assert props["baseline_frp_mw"] == 50.0
        # 500 / 50 = 10.0x
        assert props["anomaly_factor"] == 10.0

    @patch("ember.services.firms.httpx.AsyncClient")
    async def test_novel_event_no_history(self, mock_client_class, service):
        """Cluster with no archive history gets null anomaly_factor."""
        current_csv = _make_csv(
            [
                {
                    "latitude": "35.5",
                    "longitude": "-102.3",
                    "bright_ti4": "350",
                    "frp": "500",
                    "confidence": "nominal",
                    "acq_date": "2026-04-06",
                    "acq_time": "1420",
                    "satellite": "N",
                    "daynight": "D",
                },
            ]
        )

        # Empty archive
        empty_csv = _make_csv([])

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[
                MagicMock(text=current_csv, raise_for_status=MagicMock()),
                MagicMock(text=empty_csv, raise_for_status=MagicMock()),
                MagicMock(text=empty_csv, raise_for_status=MagicMock()),
            ]
        )
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.get_fires(min_lat=35.0, max_lat=36.0, min_lon=-103.0, max_lon=-101.0)

        features = result["geojson"]["features"]
        marker = next(f for f in features if f["properties"]["layer"] == "marker")
        props = marker["properties"]

        assert props["anomaly_factor"] is None
        assert props["baseline_frp_mw"] == 0

    @patch("ember.services.firms.httpx.AsyncClient")
    async def test_archive_failure_graceful_degradation(self, mock_client_class, service):
        """Archive failure doesn't break clustering — anomaly_factor is null."""
        current_csv = _make_csv(
            [
                {
                    "latitude": "35.5",
                    "longitude": "-102.3",
                    "bright_ti4": "350",
                    "frp": "500",
                    "confidence": "nominal",
                    "acq_date": "2026-04-06",
                    "acq_time": "1420",
                    "satellite": "N",
                    "daynight": "D",
                },
            ]
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[
                # Current fires — success
                MagicMock(text=current_csv, raise_for_status=MagicMock()),
                # Archive window 1 — failure
                Exception("FIRMS archive timeout"),
                # Archive window 2 — failure
                Exception("FIRMS archive timeout"),
            ]
        )
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.get_fires(min_lat=35.0, max_lat=36.0, min_lon=-103.0, max_lon=-101.0)

        # Clustering should still work
        assert result["cluster_count"] >= 1
        features = result["geojson"]["features"]
        marker = next(f for f in features if f["properties"]["layer"] == "marker")
        props = marker["properties"]

        # Anomaly data should be null, not missing
        assert "anomaly_factor" in props
        assert "baseline_frp_mw" in props


# ============================================================================
# Archive caching
# ============================================================================


class TestBaselineCaching:
    """Test that archive queries are cached."""

    @patch("ember.services.firms.httpx.AsyncClient")
    async def test_archive_cached_on_second_call(self, mock_client_class, service):
        """Second archive query for same bbox hits cache."""
        archive_csv = _make_csv(
            [
                {
                    "latitude": "35.5",
                    "longitude": "-102.3",
                    "bright_ti4": "300",
                    "frp": "50",
                    "confidence": "nominal",
                    "acq_date": "2026-03-28",
                    "acq_time": "1400",
                    "satellite": "N",
                    "daynight": "D",
                },
            ]
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=MagicMock(text=archive_csv, raise_for_status=MagicMock())
        )
        mock_client_class.return_value.__aenter__.return_value = mock_client

        # First call — hits FIRMS
        result1 = await service._get_archive_detections(
            35.0, 36.0, -103.0, -101.0, "VIIRS_SNPP_NRT"
        )
        assert len(result1) > 0

        # Second call — should hit cache (no new HTTP calls)
        call_count_before = mock_client.get.call_count
        result2 = await service._get_archive_detections(
            35.0, 36.0, -103.0, -101.0, "VIIRS_SNPP_NRT"
        )
        assert result2 == result1
        assert mock_client.get.call_count == call_count_before


# ============================================================================
# Distance filtering
# ============================================================================


class TestDistanceFiltering:
    """Test that archive detections are filtered by radius."""

    @patch("ember.services.firms.httpx.AsyncClient")
    async def test_far_detections_excluded(self, mock_client_class, service):
        """Archive detections far from centroid are excluded from baseline."""
        current_csv = _make_csv(
            [
                {
                    "latitude": "35.5",
                    "longitude": "-102.3",
                    "bright_ti4": "350",
                    "frp": "100",
                    "confidence": "nominal",
                    "acq_date": "2026-04-06",
                    "acq_time": "1420",
                    "satellite": "N",
                    "daynight": "D",
                },
            ]
        )

        # One nearby detection (within 5km) and one far (>100km away)
        archive_csv = _make_csv(
            [
                {
                    "latitude": "35.5",
                    "longitude": "-102.3",
                    "bright_ti4": "300",
                    "frp": "50",
                    "confidence": "nominal",
                    "acq_date": "2026-03-28",
                    "acq_time": "1400",
                    "satellite": "N",
                    "daynight": "D",
                },
                {
                    "latitude": "36.5",
                    "longitude": "-101.0",
                    "bright_ti4": "300",
                    "frp": "9999",
                    "confidence": "nominal",
                    "acq_date": "2026-03-28",
                    "acq_time": "1400",
                    "satellite": "N",
                    "daynight": "D",
                },
            ]
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=[
                MagicMock(text=current_csv, raise_for_status=MagicMock()),
                MagicMock(text=archive_csv, raise_for_status=MagicMock()),
                MagicMock(text=archive_csv, raise_for_status=MagicMock()),
            ]
        )
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.get_fires(min_lat=35.0, max_lat=36.0, min_lon=-103.0, max_lon=-101.0)

        features = result["geojson"]["features"]
        marker = next(f for f in features if f["properties"]["layer"] == "marker")
        props = marker["properties"]

        # Baseline should be 50 (nearby only), not influenced by the 9999 far detection
        assert props["baseline_frp_mw"] == 50.0
        # 100 / 50 = 2.0
        assert props["anomaly_factor"] == 2.0
