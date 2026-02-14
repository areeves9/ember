"""NASA FIRMS fire data service with DBSCAN clustering."""

import csv
from io import StringIO
from time import time
from typing import Any

import httpx
import numpy as np
from pyproj import Geod
from scipy.spatial import ConvexHull
from sklearn.cluster import DBSCAN

from ember.config import settings

# Cache for FIRMS queries (FIRMS updates every ~10 min)
_fires_cache: dict[str, dict] = {}
_FIRES_CACHE_TTL = 600  # 10 minutes
_FIRES_CACHE_MAX_SIZE = 100

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

    def _parse_csv(self, csv_text: str) -> list[dict]:
        """Parse FIRMS CSV response into list of detections."""
        detections = []
        reader = csv.DictReader(StringIO(csv_text))

        for row in reader:
            try:
                detection = {
                    "lat": float(row.get("latitude", 0)),
                    "lon": float(row.get("longitude", 0)),
                    "brightness": float(
                        row.get("bright_ti4", 0) or row.get("brightness", 0)
                    ),
                    "frp": float(row.get("frp", 0) or 0),
                    "confidence": self._normalize_confidence(
                        row.get("confidence", "nominal")
                    ),
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
            "low"
            if avg_conf_num < 1.5
            else ("high" if avg_conf_num > 2.5 else "nominal")
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
            points = np.array(
                list(zip(lons, lats))
            )  # scipy expects (x, y) = (lon, lat)
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


# Singleton instance
firms_service = FirmsService()
