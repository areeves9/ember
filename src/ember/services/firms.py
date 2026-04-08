"""NASA FIRMS fire data service with DBSCAN clustering and anomaly enrichment."""

import csv
import math
from datetime import datetime, timedelta, timezone
from io import StringIO
from time import time
from typing import Any

import httpx
import numpy as np
from pyproj import Geod
from scipy.spatial import ConvexHull
from sklearn.cluster import DBSCAN

from ember.config import settings
from ember.logging import get_logger

logger = get_logger(__name__)

# Cache for FIRMS queries (FIRMS updates every ~10 min)
_fires_cache: dict[str, dict] = {}
_FIRES_CACHE_TTL = 600  # 10 minutes
_FIRES_CACHE_MAX_SIZE = 100

# Cache for baseline archive queries (archive is slow-changing)
_baseline_cache: dict[str, dict] = {}
_BASELINE_CACHE_TTL = 21600  # 6 hours
_BASELINE_CACHE_MAX_SIZE = 50

# Cache for timeline queries (changes with each satellite pass)
_timeline_cache: dict[str, dict] = {}
_TIMELINE_CACHE_TTL = 3600  # 1 hour
_TIMELINE_CACHE_MAX_SIZE = 100

# Pass grouping: detections from same satellite within this window are one pass
_PASS_GROUP_MINUTES = 15

# FIRMS API base URL
FIRMS_BASE_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

# Available satellite sources
SATELLITE_SOURCES = [
    "VIIRS_SNPP_NRT",
    "VIIRS_NOAA20_NRT",
    "VIIRS_NOAA21_NRT",
    "MODIS_NRT",
    "GOES16_NRT",
    "GOES17_NRT",
    "GOES18_NRT",
]

# GOES sources are geostationary — not available via the FIRMS area/csv endpoint
GEOSTATIONARY_SOURCES = {"GOES16_NRT", "GOES17_NRT", "GOES18_NRT"}

# Earth radius in km (for haversine calculations)
EARTH_RADIUS_KM = 6371.0

# Default clustering radius in km
DEFAULT_CLUSTER_RADIUS_KM = 1.0

# VIIRS pixel size in km² (for single detection area estimate)
VIIRS_PIXEL_KM2 = 0.14


class FirmsService:
    """Service for fetching NASA FIRMS active fire data with clustering."""

    def __init__(self):
        self.api_key = settings.firms_map_key
        self.timeout = settings.http_timeout

    async def get_fires(
        self,
        min_lat: float,
        max_lat: float,
        min_lon: float,
        max_lon: float,
        source: str = "VIIRS_SNPP_NRT",
        days_back: int = 2,
        cluster_radius_km: float = DEFAULT_CLUSTER_RADIUS_KM,
    ) -> dict[str, Any]:
        """
        Fetch active fire detections for a bounding box with clustering.

        Args:
            min_lat: Southern boundary
            max_lat: Northern boundary
            min_lon: Western boundary
            max_lon: Eastern boundary
            source: Satellite source (VIIRS_SNPP_NRT, MODIS_NRT, etc.)
            days_back: Days of data (1-10)
            cluster_radius_km: Clustering radius in km (default 1.0)

        Returns:
            Dict with clustered detections and GeoJSON for map rendering
        """
        if not self.api_key:
            raise ValueError("FIRMS_MAP_KEY not configured")

        if source not in SATELLITE_SOURCES:
            raise ValueError(f"Invalid source. Must be one of: {SATELLITE_SOURCES}")

        if source in GEOSTATIONARY_SOURCES:
            return {
                "source": source,
                "days_back": days_back,
                "bbox": {
                    "min_lat": min_lat,
                    "max_lat": max_lat,
                    "min_lon": min_lon,
                    "max_lon": max_lon,
                },
                "detection_count": 0,
                "cluster_count": 0,
                "cluster_radius_km": cluster_radius_km,
                "detections": [],
                "geojson": {"type": "FeatureCollection", "features": []},
                "message": (
                    f"{source} is geostationary — fire detections are not available"
                    " via the FIRMS area endpoint."
                ),
            }

        days_back = max(1, min(10, days_back))

        # Check cache (round bbox to 2 decimals for reasonable grouping)
        cache_key = f"fires:{min_lat:.2f},{max_lat:.2f},{min_lon:.2f},{max_lon:.2f}:{source}:{days_back}:{cluster_radius_km}"
        cached = _fires_cache.get(cache_key)
        if cached and (time() - cached["timestamp"] < _FIRES_CACHE_TTL):
            return cached["data"]

        # Build bounding box string: west,south,east,north
        bbox = f"{min_lon},{min_lat},{max_lon},{max_lat}"

        url = f"{FIRMS_BASE_URL}/{self.api_key}/{source}/{bbox}/{days_back}"

        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=self.timeout)
            response.raise_for_status()

        # Parse CSV response
        detections = self._parse_csv(response.text)

        # Cluster detections using DBSCAN
        clusters = self._cluster_detections(detections, cluster_radius_km)

        # Enrich clusters with anomaly baseline (non-blocking on failure)
        await self._enrich_baselines(clusters, min_lat, max_lat, min_lon, max_lon, source)

        # Convert to GeoJSON for map rendering
        geojson = self._build_geojson(clusters)

        result = {
            "source": source,
            "days_back": days_back,
            "bbox": {
                "min_lat": min_lat,
                "max_lat": max_lat,
                "min_lon": min_lon,
                "max_lon": max_lon,
            },
            "detection_count": len(detections),
            "cluster_count": len(clusters),
            "cluster_radius_km": cluster_radius_km,
            "detections": detections,
            "geojson": geojson,
        }

        # Store in cache
        if len(_fires_cache) >= _FIRES_CACHE_MAX_SIZE:
            _fires_cache.clear()
        _fires_cache[cache_key] = {"timestamp": time(), "data": result}

        return result

    # ------------------------------------------------------------------
    # Anomaly baseline enrichment
    # ------------------------------------------------------------------

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Haversine distance in km between two points."""
        rlat1, rlon1 = math.radians(lat1), math.radians(lon1)
        rlat2, rlon2 = math.radians(lat2), math.radians(lon2)
        dlat = rlat2 - rlat1
        dlon = rlon2 - rlon1
        a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
        return EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(a))

    async def _get_archive_detections(
        self,
        min_lat: float,
        max_lat: float,
        min_lon: float,
        max_lon: float,
        source: str,
        days: int = 10,
    ) -> list[dict]:
        """Fetch FIRMS archive detections for a bbox over the past N days.

        Makes 2 FIRMS requests (2 × 5-day windows) to cover 10 days.
        Results are cached by rounded bbox+source with 6-hour TTL.

        Returns list of detection dicts with lat, lon, frp keys.
        """
        # Cache key (round bbox to 1 decimal ~11km for archive grouping)
        cache_key = f"baseline:{min_lat:.1f},{max_lat:.1f},{min_lon:.1f},{max_lon:.1f}:{source}"
        cached = _baseline_cache.get(cache_key)
        if cached and (time() - cached["timestamp"] < _BASELINE_CACHE_TTL):
            return cached["data"]

        # Compute date windows: [10d ago → 5d ago] + [5d ago → today]
        now = datetime.now(timezone.utc)
        date_10d = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        date_5d = (now - timedelta(days=days // 2)).strftime("%Y-%m-%d")

        bbox_str = f"{min_lon},{min_lat},{max_lon},{max_lat}"
        all_detections: list[dict] = []

        for date_param in [date_10d, date_5d]:
            url = f"{FIRMS_BASE_URL}/{self.api_key}/{source}/{bbox_str}/{days // 2}/{date_param}"
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, timeout=self.timeout)
                    resp.raise_for_status()
                all_detections.extend(self._parse_csv(resp.text))
            except Exception as exc:
                logger.warning("FIRMS archive query failed: %s", exc)

        # Cache results
        if len(_baseline_cache) >= _BASELINE_CACHE_MAX_SIZE:
            _baseline_cache.clear()
        _baseline_cache[cache_key] = {
            "timestamp": time(),
            "data": all_detections,
        }

        return all_detections

    async def _enrich_baselines(
        self,
        clusters: list[dict],
        min_lat: float,
        max_lat: float,
        min_lon: float,
        max_lon: float,
        source: str,
        radius_km: float = 5.0,
    ) -> None:
        """Enrich clusters with anomaly_factor and baseline_frp_mw.

        Fetches archive detections once for the entire viewport bbox,
        then filters per-cluster by distance from centroid. Mutates
        cluster dicts in-place. Gracefully degrades on failure.
        """
        try:
            # Pad bbox by radius_km to catch edge detections
            pad_deg = radius_km / 111.32
            archive = await self._get_archive_detections(
                min_lat - pad_deg,
                max_lat + pad_deg,
                min_lon - pad_deg,
                max_lon + pad_deg,
                source,
            )
        except Exception as exc:
            logger.warning("Baseline enrichment failed: %s", exc)
            for cluster in clusters:
                cluster["anomaly_factor"] = None
                cluster["baseline_frp_mw"] = None
            return

        for cluster in clusters:
            clat = cluster["centroid_lat"]
            clon = cluster["centroid_lon"]
            current_frp = cluster["total_frp"]

            # Filter archive detections within radius_km of centroid
            nearby_frps = [
                d["frp"]
                for d in archive
                if self._haversine_km(d["lat"], d["lon"], clat, clon) <= radius_km
            ]

            if not nearby_frps:
                # Novel event — no thermal history
                cluster["anomaly_factor"] = None
                cluster["baseline_frp_mw"] = 0
                continue

            baseline_frp = sum(nearby_frps) / len(nearby_frps)
            if baseline_frp > 0:
                cluster["anomaly_factor"] = round(current_frp / baseline_frp, 1)
            else:
                cluster["anomaly_factor"] = None
            cluster["baseline_frp_mw"] = round(baseline_frp, 1)

    def _parse_csv(self, csv_text: str) -> list[dict]:
        """Parse FIRMS CSV response into list of detections."""
        detections = []
        reader = csv.DictReader(StringIO(csv_text))

        for row in reader:
            try:
                detection = {
                    "lat": float(row.get("latitude", 0)),
                    "lon": float(row.get("longitude", 0)),
                    "brightness": float(row.get("bright_ti4", 0) or row.get("brightness", 0)),
                    "frp": float(row.get("frp", 0) or 0),
                    "confidence": self._normalize_confidence(row.get("confidence", "nominal")),
                    "acq_date": row.get("acq_date", ""),
                    "acq_time": row.get("acq_time", ""),
                    "satellite": row.get("satellite", ""),
                    "daynight": row.get("daynight", ""),
                }
                detections.append(detection)
            except (ValueError, KeyError):
                continue

        return detections

    def _normalize_confidence(self, conf: str) -> str:
        """Normalize confidence value to low/nominal/high."""
        conf_lower = str(conf).lower()
        if conf_lower in ("l", "low"):
            return "low"
        elif conf_lower in ("h", "high"):
            return "high"
        else:
            return "nominal"

    def _cluster_detections(
        self, detections: list[dict], radius_km: float = DEFAULT_CLUSTER_RADIUS_KM
    ) -> list[dict]:
        """
        Cluster nearby fire detections using DBSCAN.

        Args:
            detections: List of fire detection records
            radius_km: Clustering radius in kilometers

        Returns:
            List of cluster dictionaries with aggregated stats
        """
        if not detections:
            return []

        if len(detections) == 1:
            # Single detection - return as cluster of 1
            det = detections[0]
            return [self._make_cluster(0, [det])]

        # Extract coordinates for clustering
        coords = np.array([[d["lat"], d["lon"]] for d in detections])

        # Convert radius to radians for haversine metric
        eps_radians = radius_km / EARTH_RADIUS_KM

        # DBSCAN with haversine metric (expects radians)
        coords_rad = np.radians(coords)
        db = DBSCAN(eps=eps_radians, min_samples=1, metric="haversine")
        labels = db.fit_predict(coords_rad)

        # Group detections by cluster label
        cluster_groups: dict[int, list[dict]] = {}
        for i, label in enumerate(labels):
            if label not in cluster_groups:
                cluster_groups[label] = []
            cluster_groups[label].append(detections[i])

        # Build cluster objects
        clusters = []
        for cluster_id, group in cluster_groups.items():
            clusters.append(self._make_cluster(int(cluster_id), group))

        return clusters

    def _make_cluster(self, cluster_id: int, detections: list[dict]) -> dict:
        """
        Create a cluster object from a group of detections.

        Calculates centroid, aggregate stats, convex hull, and area.
        """
        lats = [d["lat"] for d in detections]
        lons = [d["lon"] for d in detections]
        frps = [d["frp"] for d in detections]
        brightnesses = [d["brightness"] for d in detections]

        # Centroid
        centroid_lat = sum(lats) / len(lats)
        centroid_lon = sum(lons) / len(lons)

        # Aggregate confidence
        confidence_map = {"low": 1, "nominal": 2, "high": 3}
        confidences = [confidence_map.get(d["confidence"], 2) for d in detections]
        avg_conf_num = sum(confidences) / len(confidences)
        avg_confidence = (
            "low" if avg_conf_num < 1.5 else ("high" if avg_conf_num > 2.5 else "nominal")
        )

        # Time range
        datetimes = []
        for d in detections:
            if d["acq_date"] and d["acq_time"]:
                dt_str = f"{d['acq_date']}T{d['acq_time'][:2]}:{d['acq_time'][2:]}:00Z"
                datetimes.append(dt_str)
            elif d["acq_date"]:
                datetimes.append(d["acq_date"])

        earliest = min(datetimes) if datetimes else ""
        latest = max(datetimes) if datetimes else ""

        # Area and convex hull
        area_km2 = VIIRS_PIXEL_KM2  # Default for single detection
        hull_vertices = None

        if len(detections) == 2:
            # Two detections - estimate as line with buffer
            area_km2 = VIIRS_PIXEL_KM2 * 2
        elif len(detections) >= 3:
            # Compute convex hull
            hull_vertices = self._compute_convex_hull(lats, lons)
            if hull_vertices and len(hull_vertices) >= 3:
                area_km2 = self._compute_geodesic_area(hull_vertices)

        return {
            "cluster_id": cluster_id,
            "centroid_lat": round(centroid_lat, 6),
            "centroid_lon": round(centroid_lon, 6),
            "detection_count": len(detections),
            "total_frp": round(sum(frps), 2),
            "max_brightness": round(max(brightnesses), 2),
            "avg_confidence": avg_confidence,
            "area_km2": round(area_km2, 4),
            "hull_vertices": hull_vertices,
            "earliest": earliest,
            "latest": latest,
        }

    def _compute_convex_hull(
        self, lats: list[float], lons: list[float]
    ) -> list[tuple[float, float]] | None:
        """
        Compute convex hull of points.

        Returns list of (lat, lon) vertices in counter-clockwise order, or None if hull cannot be computed.
        """
        if len(lats) < 3:
            return None

        try:
            points = np.array(list(zip(lons, lats)))  # scipy expects (x, y) = (lon, lat)
            hull = ConvexHull(points)
            vertices = [(lats[i], lons[i]) for i in hull.vertices]
            # Close the polygon
            vertices.append(vertices[0])
            return vertices
        except Exception:
            # Collinear points or other issues
            return None

    def _compute_geodesic_area(self, vertices: list[tuple[float, float]]) -> float:
        """
        Compute geodesic area of polygon in km².

        Uses WGS84 ellipsoid for accurate Earth surface calculations.
        """
        if not vertices or len(vertices) < 4:  # Need at least 3 unique + closing vertex
            return VIIRS_PIXEL_KM2

        try:
            geod = Geod(ellps="WGS84")
            lats = [v[0] for v in vertices]
            lons = [v[1] for v in vertices]
            area_m2, _ = geod.polygon_area_perimeter(lons, lats)
            area_km2 = abs(area_m2) / 1_000_000
            return max(area_km2, VIIRS_PIXEL_KM2)  # Minimum of one pixel
        except Exception:
            return VIIRS_PIXEL_KM2

    def _build_geojson(self, clusters: list[dict]) -> dict:
        """
        Build GeoJSON FeatureCollection from clusters.

        Emits dual geometry for each cluster:
        - Point feature (layer="marker") for centroid
        - Polygon feature (layer="area") for convex hull (if 3+ detections)
        """
        features = []

        for cluster in clusters:
            shared_props = {
                "cluster_id": cluster["cluster_id"],
                "detection_count": cluster["detection_count"],
                "total_frp": cluster["total_frp"],
                "max_brightness": cluster["max_brightness"],
                "confidence": cluster["avg_confidence"],
                "area_km2": cluster["area_km2"],
                "earliest": cluster["earliest"],
                "latest": cluster["latest"],
                "anomaly_factor": cluster.get("anomaly_factor"),
                "baseline_frp_mw": cluster.get("baseline_frp_mw"),
            }

            # Point feature for centroid marker
            marker_feature = {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [cluster["centroid_lon"], cluster["centroid_lat"]],
                },
                "properties": {
                    **shared_props,
                    "layer": "marker",
                },
            }
            features.append(marker_feature)

            # Polygon feature for convex hull (if available)
            hull = cluster.get("hull_vertices")
            if hull and len(hull) >= 4:
                ring = [[lon, lat] for lat, lon in hull]
                area_feature = {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [ring],
                    },
                    "properties": {
                        **shared_props,
                        "layer": "area",
                    },
                }
                features.append(area_feature)

        return {
            "type": "FeatureCollection",
            "features": features,
        }

    # ------------------------------------------------------------------
    # Thermal timeline
    # ------------------------------------------------------------------

    async def get_timeline(
        self,
        lat: float,
        lon: float,
        radius_km: float = 5.0,
        hours: int = 72,
        source: str = "VIIRS_SNPP_NRT",
    ) -> dict[str, Any]:
        """Get thermal timeline for a location — FRP over time by satellite pass.

        Args:
            lat: Center latitude
            lon: Center longitude
            radius_km: Search radius in km (default 5)
            hours: Lookback window (default 72, max 120 = 5 days)
            source: Satellite source filter

        Returns:
            Dict with observations grouped by satellite pass, trend, peak.
        """
        if not self.api_key:
            raise ValueError("FIRMS_MAP_KEY not configured")

        hours = max(1, min(120, hours))

        # Cache check
        cache_key = f"timeline:{lat:.3f},{lon:.3f}:{radius_km}:{hours}:{source}"
        cached = _timeline_cache.get(cache_key)
        if cached and (time() - cached["timestamp"] < _TIMELINE_CACHE_TTL):
            return cached["data"]

        # Compute bbox from lat/lon + radius
        pad_deg = radius_km / 111.32
        min_lat = lat - pad_deg
        max_lat = lat + pad_deg
        min_lon = lon - pad_deg
        max_lon = lon + pad_deg
        bbox_str = f"{min_lon},{min_lat},{max_lon},{max_lat}"

        # Compute days needed (ceil to whole days, max 5 per request)
        days = max(1, min(5, -(-hours // 24)))  # ceil division

        # Fetch detections (1-2 requests depending on hours)
        all_detections: list[dict] = []
        now = datetime.now(timezone.utc)

        if hours <= 120:
            # Up to 5 days fits in one request
            date_start = (now - timedelta(hours=hours)).strftime("%Y-%m-%d")
            url = f"{FIRMS_BASE_URL}/{self.api_key}/{source}/{bbox_str}/{days}/{date_start}"
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, timeout=self.timeout)
                    resp.raise_for_status()
                all_detections = self._parse_csv(resp.text)
            except Exception as exc:
                logger.warning("FIRMS timeline query failed: %s", exc)
                raise ValueError(f"FIRMS API error: {exc}") from exc

        # Filter by radius
        nearby = [
            d
            for d in all_detections
            if self._haversine_km(d["lat"], d["lon"], lat, lon) <= radius_km
        ]

        if not nearby:
            result: dict[str, Any] = {
                "status": "success",
                "first_detected": None,
                "hours_active": 0,
                "observations": [],
                "trend": None,
                "peak": None,
                "source": "NASA FIRMS",
            }
            self._cache_timeline(cache_key, result)
            return result

        # Group by satellite pass
        observations = self._group_by_pass(nearby)

        # Compute metadata
        first_detected = observations[0]["time"]
        peak = max(observations, key=lambda o: o["frp_mw"])

        # Hours active
        first_dt = datetime.fromisoformat(first_detected.replace("Z", "+00:00"))
        hours_active = round((now - first_dt).total_seconds() / 3600, 1)

        # Trend
        trend = self._classify_trend(observations)

        result = {
            "status": "success",
            "first_detected": first_detected,
            "hours_active": hours_active,
            "observations": observations,
            "trend": trend,
            "peak": {
                "time": peak["time"],
                "frp_mw": peak["frp_mw"],
                "satellite": peak["satellite"],
            },
            "source": "NASA FIRMS",
        }

        self._cache_timeline(cache_key, result)
        return result

    def _group_by_pass(self, detections: list[dict]) -> list[dict]:
        """Group detections into satellite passes.

        Detections from the same satellite within 15 minutes are one pass.
        Returns list of observations sorted by time.
        """
        # Parse and sort by datetime
        timed: list[tuple[datetime, dict]] = []
        for d in detections:
            if d["acq_date"] and d["acq_time"]:
                dt_str = f"{d['acq_date']}T{d['acq_time'][:2]}:{d['acq_time'][2:]}:00Z"
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                timed.append((dt, d))

        if not timed:
            return []

        timed.sort(key=lambda x: x[0])

        # Group by satellite + time proximity
        passes: list[dict] = []
        current_group: list[tuple[datetime, dict]] = [timed[0]]

        for i in range(1, len(timed)):
            dt, det = timed[i]
            prev_dt, prev_det = current_group[-1]
            same_sat = det["satellite"] == prev_det["satellite"]
            within_window = (dt - prev_dt).total_seconds() <= _PASS_GROUP_MINUTES * 60

            if same_sat and within_window:
                current_group.append((dt, det))
            else:
                passes.append(self._aggregate_pass(current_group))
                current_group = [(dt, det)]

        passes.append(self._aggregate_pass(current_group))
        passes.sort(key=lambda p: p["time"])
        return passes

    @staticmethod
    def _aggregate_pass(
        group: list[tuple[datetime, dict]],
    ) -> dict:
        """Aggregate a group of detections into one observation."""
        frps = [d["frp"] for _, d in group]
        mid_dt = group[len(group) // 2][0]
        satellite = group[0][1]["satellite"]

        # Map satellite codes to readable names
        sat_names = {
            "N": "VIIRS NOAA-20",
            "1": "VIIRS NOAA-21",
            "N20": "VIIRS NOAA-20",
            "N21": "VIIRS NOAA-21",
            "Terra": "MODIS Terra",
            "Aqua": "MODIS Aqua",
        }

        return {
            "time": mid_dt.strftime("%Y-%m-%dT%H:%MZ"),
            "frp_mw": round(sum(frps), 1),
            "detections": len(group),
            "satellite": sat_names.get(satellite, satellite),
        }

    @staticmethod
    def _classify_trend(observations: list[dict]) -> str:
        """Classify FRP trend from observations.

        Uses last 3 observations to determine direction.
        """
        if len(observations) < 2:
            return "sporadic"

        recent = observations[-3:] if len(observations) >= 3 else observations
        frps = [o["frp_mw"] for o in recent]

        # Check monotonic trends
        increasing = all(frps[i] < frps[i + 1] for i in range(len(frps) - 1))
        decreasing = all(frps[i] > frps[i + 1] for i in range(len(frps) - 1))

        if increasing:
            return "escalating"
        if decreasing:
            return "declining"

        # Check stability (within ±20%) before peaked — small variations aren't peaks
        mean_frp = sum(frps) / len(frps)
        if mean_frp > 0:
            all_within = all(abs(f - mean_frp) / mean_frp <= 0.2 for f in frps)
            if all_within:
                return "stable"

        # Check if peaked (was rising, now falling)
        if len(observations) >= 3:
            all_frps = [o["frp_mw"] for o in observations]
            peak_idx = all_frps.index(max(all_frps))
            if 0 < peak_idx < len(all_frps) - 1:
                return "peaked"

        return "sporadic"

    @staticmethod
    def _cache_timeline(cache_key: str, data: dict) -> None:
        """Store timeline result in cache."""
        if len(_timeline_cache) >= _TIMELINE_CACHE_MAX_SIZE:
            _timeline_cache.clear()
        _timeline_cache[cache_key] = {"timestamp": time(), "data": data}


# Singleton instance
firms_service = FirmsService()
