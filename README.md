<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker">
</p>

# 🔥 Ember

**Wildfire intelligence API that turns massive raster datasets into fast, queryable endpoints.**

Ember serves [Cloud Optimized GeoTIFFs](https://www.cogeo.org/) (COGs) via REST — no GIS software required. A 3GB LANDFIRE raster becomes a ~50KB HTTP range request. Query terrain data at a point, or extract a viewport as a GeoTIFF for client-side rendering.

It also aggregates NASA FIRMS satellite fire detections, clustering nearby hotspots with DBSCAN and computing convex hulls with geodesic area calculations.

```
┌─────────────────┐     ┌──────────────┐     ┌─────────────────────┐
│  GET /terrain   │ ──► │  rio-tiler   │ ──► │  S3 COG             │
│  ?lat=34&lon=-118    │  point/part  │     │  (HTTP Range GET)   │
└─────────────────┘     └──────────────┘     └─────────────────────┘
                              │
                              │ ~50KB (not 3GB)
                              ▼
                       ┌──────────────┐
                       │ Pixel Value  │
                       │ or GeoTIFF   │
                       └──────────────┘
```

---

## 📡 Endpoints

| Endpoint | Source | Description |
|:---------|:-------|:------------|
| `/api/v1/fires` | NASA FIRMS | Fire detections with DBSCAN clustering, convex hulls, GeoJSON output |
| `/api/v1/terrain` | LANDFIRE S3 | Elevation, slope, aspect, fuel model, canopy metrics (point or raster) |
| `/api/v1/fuel` | LANDFIRE | FBFM40 fuel model codes (Scott & Burgan classification) |
| `/api/v1/weather` | Open-Meteo | Current conditions and forecasts |
| `/api/v1/vegetation` | Copernicus | NDVI/NDMI vegetation indices |
| `/api/v1/geocode` | Nominatim | Forward/reverse geocoding |

---

## 🗺️ Terrain Layers

Query any LANDFIRE layer at a point or as a raster for a bounding box:

| Layer | Description | Units |
|:------|:------------|:------|
| `elevation` | Surface elevation | meters |
| `slope` | Terrain slope | degrees (0-90) |
| `aspect` | Slope direction | degrees (0-360) + cardinal |
| `fuel` | FBFM40 fuel model | Scott & Burgan code |
| `canopy_cover` | Canopy cover | percent |
| `canopy_height` | Canopy height | meters |
| `canopy_base_height` | Height to live crown | meters |
| `canopy_bulk_density` | Crown fuel density | kg/m³ |

<details>
<summary><b>Example requests</b></summary>

```bash
# Point query — returns JSON
curl "localhost:8001/api/v1/terrain?lat=34.05&lon=-118.25"

# Raster query (bbox crop) — returns base64 GeoTIFF
curl "localhost:8001/api/v1/terrain?min_lat=34&max_lat=34.5&min_lon=-118.5&max_lon=-118&layers=elevation&format=raster"

# Raster query (full CONUS, no bbox) — pyramid overview, Cache-Control: 24h
curl "localhost:8001/api/v1/terrain?format=raster&layers=elevation"
```

</details>

---

## 🚀 Quick Start

```bash
# Clone and configure
cp .env.example .env
```

**Required environment variables:**
| Variable | Description |
|:---------|:------------|
| `FIRMS_MAP_KEY` | [NASA FIRMS API key](https://firms.modaps.eosdis.nasa.gov/api/area/) |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_JWT_SECRET` | Supabase JWT secret |

**Optional:**
| Variable | Description |
|:---------|:------------|
| `AWS_ACCESS_KEY_ID` | For S3 COG access |
| `AWS_SECRET_ACCESS_KEY` | For S3 COG access |
| `COPERNICUS_CLIENT_ID` | For vegetation indices |
| `COPERNICUS_CLIENT_SECRET` | For vegetation indices |

```bash
# Install and run
uv sync --frozen
uv run python entrypoint.py  # → localhost:8001
```

---

## 🐳 Docker

```bash
docker compose up --build
```

---

## 🧪 Testing

```bash
pytest tests/
```

---

## 🏗️ Architecture

> See [`docs/COG_PIPELINE.md`](docs/COG_PIPELINE.md) for details on how COG queries work.

| Component | Technology |
|:----------|:-----------|
| **API Framework** | FastAPI + async/await |
| **COG Queries** | rio-tiler + rasterio (S3 range requests) |
| **Fire Clustering** | scikit-learn DBSCAN with haversine metric |
| **Area Calculations** | pyproj geodesic on WGS84 ellipsoid |
| **Caching** | In-memory TTL cache per service |

```
src/ember/
├── services/    # External API clients with caching
├── routers/     # FastAPI endpoints
└── api/         # Middleware and error handlers
```
