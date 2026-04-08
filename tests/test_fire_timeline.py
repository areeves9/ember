"""Tests for the fire thermal timeline endpoint.

Covers:
- Satellite pass grouping (same satellite within 15 min)
- Trend classification (escalating/stable/declining/peaked/sporadic)
- Peak and first_detected identification
- Empty results (no detections in radius)
- Timeline caching
- Distance filtering
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("FIRMS_MAP_KEY", "test_key")

from ember.services.firms import FirmsService, _timeline_cache

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def clear_caches():
    """Clear timeline cache before and after each test."""
    _timeline_cache.clear()
    yield
    _timeline_cache.clear()


@pytest.fixture
def service():
    svc = FirmsService()
    svc.api_key = "test_key"
    return svc


def _make_csv(rows: list[dict]) -> str:
    if not rows:
        return "latitude,longitude,bright_ti4,frp,confidence,acq_date,acq_time,satellite,daynight\n"
    headers = list(rows[0].keys())
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(str(row[h]) for h in headers))
    return "\n".join(lines)


def _det(lat, lon, frp, date, time_str, satellite="N"):
    return {
        "latitude": str(lat),
        "longitude": str(lon),
        "bright_ti4": "300",
        "frp": str(frp),
        "confidence": "nominal",
        "acq_date": date,
        "acq_time": time_str,
        "satellite": satellite,
        "daynight": "D",
    }


# ============================================================================
# Pass grouping
# ============================================================================


class TestPassGrouping:
    """Test that detections are grouped into satellite passes."""

    @patch("ember.services.firms.httpx.AsyncClient")
    async def test_same_satellite_within_window_grouped(self, mock_client_class, service):
        """Detections from same satellite within 15 min become one observation."""
        csv = _make_csv(
            [
                _det(35.5, -102.3, 100, "2026-04-06", "1420"),
                _det(35.51, -102.31, 150, "2026-04-06", "1425"),
                _det(35.49, -102.29, 80, "2026-04-06", "1430"),
            ]
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=MagicMock(text=csv, raise_for_status=MagicMock()))
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.get_timeline(lat=35.5, lon=-102.3, hours=24)

        assert result["status"] == "success"
        assert len(result["observations"]) == 1
        obs = result["observations"][0]
        assert obs["detections"] == 3
        assert obs["frp_mw"] == 330.0  # 100 + 150 + 80

    @patch("ember.services.firms.httpx.AsyncClient")
    async def test_different_satellites_separate_passes(self, mock_client_class, service):
        """Detections from different satellites at same time are separate passes."""
        csv = _make_csv(
            [
                _det(35.5, -102.3, 100, "2026-04-06", "1420", satellite="N"),
                _det(35.5, -102.3, 200, "2026-04-06", "1422", satellite="1"),
            ]
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=MagicMock(text=csv, raise_for_status=MagicMock()))
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.get_timeline(lat=35.5, lon=-102.3, hours=24)

        assert len(result["observations"]) == 2

    @patch("ember.services.firms.httpx.AsyncClient")
    async def test_same_satellite_beyond_window_separate(self, mock_client_class, service):
        """Detections from same satellite >15 min apart are separate passes."""
        csv = _make_csv(
            [
                _det(35.5, -102.3, 100, "2026-04-06", "1400"),
                _det(35.5, -102.3, 200, "2026-04-06", "1500"),
            ]
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=MagicMock(text=csv, raise_for_status=MagicMock()))
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.get_timeline(lat=35.5, lon=-102.3, hours=24)

        assert len(result["observations"]) == 2

    @patch("ember.services.firms.httpx.AsyncClient")
    async def test_3digit_acq_time_parsed_correctly(self, mock_client_class, service):
        """FIRMS acq_time like '920' (09:20) is zero-padded, not '92:0'."""
        csv = _make_csv(
            [
                _det(35.5, -102.3, 100, "2026-04-06", "920"),
            ]
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=MagicMock(text=csv, raise_for_status=MagicMock()))
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.get_timeline(lat=35.5, lon=-102.3, hours=24)

        assert result["status"] == "success"
        assert len(result["observations"]) == 1
        # Should be 09:20, not 92:0
        assert "09:20" in result["observations"][0]["time"]


# ============================================================================
# Trend classification
# ============================================================================


class TestTrendClassification:
    """Test trend classification from observations."""

    def test_escalating(self, service):
        obs = [
            {"frp_mw": 100, "time": "t1"},
            {"frp_mw": 200, "time": "t2"},
            {"frp_mw": 400, "time": "t3"},
        ]
        assert service._classify_trend(obs) == "escalating"

    def test_declining(self, service):
        obs = [
            {"frp_mw": 400, "time": "t1"},
            {"frp_mw": 200, "time": "t2"},
            {"frp_mw": 100, "time": "t3"},
        ]
        assert service._classify_trend(obs) == "declining"

    def test_stable(self, service):
        obs = [
            {"frp_mw": 100, "time": "t1"},
            {"frp_mw": 105, "time": "t2"},
            {"frp_mw": 98, "time": "t3"},
        ]
        assert service._classify_trend(obs) == "stable"

    def test_peaked(self, service):
        obs = [
            {"frp_mw": 100, "time": "t1"},
            {"frp_mw": 500, "time": "t2"},
            {"frp_mw": 200, "time": "t3"},
        ]
        assert service._classify_trend(obs) == "peaked"

    def test_sporadic(self, service):
        obs = [
            {"frp_mw": 100, "time": "t1"},
            {"frp_mw": 500, "time": "t2"},
            {"frp_mw": 50, "time": "t3"},
            {"frp_mw": 800, "time": "t4"},
        ]
        assert service._classify_trend(obs) == "sporadic"

    def test_single_observation(self, service):
        obs = [{"frp_mw": 100, "time": "t1"}]
        assert service._classify_trend(obs) == "sporadic"


# ============================================================================
# Peak and first_detected
# ============================================================================


class TestTimelineMetadata:
    """Test peak and first_detected identification."""

    @patch("ember.services.firms.httpx.AsyncClient")
    async def test_peak_identified(self, mock_client_class, service):
        """Peak observation has highest FRP."""
        csv = _make_csv(
            [
                _det(35.5, -102.3, 100, "2026-04-04", "1400"),
                _det(35.5, -102.3, 500, "2026-04-05", "1400"),
                _det(35.5, -102.3, 200, "2026-04-06", "1400"),
            ]
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=MagicMock(text=csv, raise_for_status=MagicMock()))
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.get_timeline(lat=35.5, lon=-102.3, hours=72)

        assert result["peak"]["frp_mw"] == 500.0
        assert "2026-04-05" in result["peak"]["time"]

    @patch("ember.services.firms.httpx.AsyncClient")
    async def test_first_detected(self, mock_client_class, service):
        """first_detected is the earliest observation."""
        csv = _make_csv(
            [
                _det(35.5, -102.3, 100, "2026-04-04", "1400"),
                _det(35.5, -102.3, 200, "2026-04-06", "1400"),
            ]
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=MagicMock(text=csv, raise_for_status=MagicMock()))
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.get_timeline(lat=35.5, lon=-102.3, hours=72)

        assert "2026-04-04" in result["first_detected"]
        assert result["hours_active"] > 0


# ============================================================================
# Empty results
# ============================================================================


class TestTimelineEmpty:
    """Test timeline with no matching detections."""

    @patch("ember.services.firms.httpx.AsyncClient")
    async def test_no_detections_returns_empty(self, mock_client_class, service):
        """No detections in radius returns empty observations."""
        csv = _make_csv([])

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=MagicMock(text=csv, raise_for_status=MagicMock()))
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.get_timeline(lat=35.5, lon=-102.3, hours=72)

        assert result["status"] == "success"
        assert result["observations"] == []
        assert result["first_detected"] is None
        assert result["trend"] is None
        assert result["peak"] is None

    @patch("ember.services.firms.httpx.AsyncClient")
    async def test_far_detections_filtered(self, mock_client_class, service):
        """Detections outside radius_km are excluded."""
        csv = _make_csv(
            [
                _det(40.0, -100.0, 500, "2026-04-06", "1400"),  # >500km away
            ]
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=MagicMock(text=csv, raise_for_status=MagicMock()))
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.get_timeline(lat=35.5, lon=-102.3, hours=72)

        assert result["observations"] == []


# ============================================================================
# Caching
# ============================================================================


class TestTimelineCaching:
    """Test timeline result caching."""

    @patch("ember.services.firms.httpx.AsyncClient")
    async def test_second_call_hits_cache(self, mock_client_class, service):
        """Second identical timeline request hits cache."""
        csv = _make_csv(
            [
                _det(35.5, -102.3, 100, "2026-04-06", "1400"),
            ]
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=MagicMock(text=csv, raise_for_status=MagicMock()))
        mock_client_class.return_value.__aenter__.return_value = mock_client

        await service.get_timeline(lat=35.5, lon=-102.3, hours=72)
        call_count = mock_client.get.call_count

        await service.get_timeline(lat=35.5, lon=-102.3, hours=72)
        assert mock_client.get.call_count == call_count  # No new calls
