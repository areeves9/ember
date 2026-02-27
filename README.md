# Ember

Wildfire intelligence API that turns massive raster datasets into fast, queryable endpoints.

## What it does

Ember serves [Cloud Optimized GeoTIFFs](https://www.cogeo.org/) (COGs) via REST - no GIS software required. A 3GB LANDFIRE raster becomes a ~50KB HTTP range request. Query terrain data at a point, or extract a viewport as a GeoTIFF for client-side rendering.

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

## Endpoints

| Endpoint | Source | Description |
|----------|--------|-------------|
| `/api/v1/fires` | NASA FIRMS | Fire detections with DBSCAN clustering, convex hulls, GeoJSON output |
| `/api/v1/terrain` | LANDFIRE S3 | Elevation, slope, aspect, fuel model, canopy metrics (point or raster) |
| `/api/v1/fuel` | LANDFIRE | FBFM40 fuel model codes (Scott & Burgan classification) |
| `/api/v1/weather` | Open-Meteo | Current conditions and forecasts |
| `/api/v1/vegetation` | Copernicus | NDVI/NDMI vegetation indices |
| `/api/v1/geocode` | Nominatim | Forward/reverse geocoding |

## Terrain layers

Query any LANDFIRE layer at a point or as a raster for a bounding box:

| Layer | Description | Units |
|-------|-------------|-------|
| `elevation` | Surface elevation | meters |
| `slope` | Terrain slope | degrees (0-90) |
| `aspect` | Slope direction | degrees (0-360) + cardinal |
| `fuel` | FBFM40 fuel model | Scott & Burgan code |
| `canopy_cover` | Canopy cover | percent |
| `canopy_height` | Canopy height | meters |
| `canopy_base_height` | Height to live crown | meters |
| `canopy_bulk_density` | Crown fuel density | kg/m³ |

```bash
# Point query - returns JSON
curl "localhost:8001/api/v1/terrain?lat=34.05&lon=-118.25"

# Raster query - returns base64 GeoTIFF
curl "localhost:8001/api/v1/terrain?min_lat=34&max_lat=34.5&min_lon=-118.5&max_lon=-118&layer=elevation&format=raster"
```

## Setup

```bash
cp .env.example .env
# Required: FIRMS_MAP_KEY (https://firms.modaps.eosdis.nasa.gov/api/area/)
# Required: SUPABASE_URL, SUPABASE_JWT_SECRET (auth)
# Optional: AWS credentials for S3 COG access
# Optional: COPERNICUS_CLIENT_ID/SECRET for vegetation

uv sync --frozen
uv run python entrypoint.py  # localhost:8001
```

## Docker

```bash
docker compose up --build
```

## Testing

```bash
pytest tests/
```

## Architecture

See `docs/COG_PIPELINE.md` for details on how COG queries work.

- **Services** (`src/ember/services/`) - External API clients with caching
- **Routers** (`src/ember/routers/`) - FastAPI endpoints
- **COG queries** - rio-tiler + rasterio for efficient S3 range requests
- **Clustering** - scikit-learn DBSCAN with haversine metric
- **Area calc** - pyproj geodesic calculations on WGS84 ellipsoid
