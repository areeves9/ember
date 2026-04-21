# Cloud Optimized GeoTIFF (COG) Pipelines

Ember reads raster data from Cloud Optimized GeoTIFFs stored on AWS S3. Two pipelines serve different data sources through the same underlying stack: **LANDFIRE** for static terrain and fuel layers, and **Sentinel-2** for dynamic satellite imagery. Both use rio-tiler for efficient HTTP range requests — only the tiles covering the requested area are fetched, not the entire file.

## Overview

### What It Does

The COG pipelines turn geographic coordinates (a point or bounding box) into raster data — pixel values, base64-encoded GeoTIFFs, or PNG images. LANDFIRE provides terrain context (elevation, slope, fuel model) for fire behavior modeling. Sentinel-2 provides satellite imagery (truecolor photos, vegetation indices) for situational awareness.

### Why It Exists

Both datasets exist as large raster files on S3. LANDFIRE layers are 1-9 GB each; the Sentinel-2 archive is petabytes. Downloading these files to answer a single query would be impractical. COG format organizes the data into internally tiled blocks with an index, enabling HTTP range requests that fetch only the ~50KB tile containing the requested pixels.

Before the Sentinel-2 COG pipeline, Ember proxied through the Copernicus Process API, which had 2-5 second latency, OAuth dependencies, rate limits, and opaque scene selection. Direct COG reads are ~100-300ms, require no credentials (public bucket), and return data from identified scenes.

### Who Uses It

The Nova frontend calls these endpoints when a user enters tactical mode and activates overlay layers (Satellite, NDVI, NDMI, Slope, Fuel, Canopy). The endpoints are also available to Pulsar's diagnostic agent for automated fire assessment.

## Architecture

### System Diagram

```
                          LANDFIRE Pipeline                    Sentinel-2 Pipeline
                    ┌─────────────────────────┐         ┌──────────────────────────────┐
                    │                         │         │                              │
 GET /terrain ──────┤  TerrainService         │         │  STACService                 │
 GET /fuel    ──────┤  (ThreadPool, 8 workers)│         │  (pystac-client, 4 workers)  │
                    │         │               │         │         │                    │
                    │         ▼               │         │         ▼                    │
                    │  COGService             │         │  Earth Search STAC API       │
                    │  (rio-tiler Reader)     │         │  (scene discovery)           │
                    │         │               │         │         │                    │
                    │         ▼               │         │         ▼                    │
                    │  Private S3 Bucket      │         │  SentinelCOGService          │
                    │  (AWS credentials)      │         │  (ThreadPool, 8 workers)     │──── GET /imagery/truecolor-cog
                    │  stellaris-landfire-data │         │         │                    │──── GET /imagery/ndvi-cog
                    │                         │         │         ▼                    │──── GET /imagery/ndmi-cog
                    └─────────────────────────┘         │  Public S3 Bucket            │──── GET /scenes/search
                                                        │  (AWS_NO_SIGN_REQUEST=YES)   │──── GET /scenes/{id}/bands
                              Shared Stack               │  sentinel-cogs               │──── GET /scenes/{id}/index
                    ┌─────────────────────────┐         │                              │
                    │  rio-tiler → rasterio   │         └──────────────────────────────┘
                    │       → GDAL            │
                    │  (HTTP range requests)  │
                    └─────────────────────────┘
```

### Components

| Component | File | Purpose |
|-----------|------|---------|
| **COGService** | `src/ember/services/cog.py` | Low-level COG reader. Point queries via rio-tiler. Coordinate transformation via pyproj. Used by LANDFIRE only. |
| **TerrainService** | `src/ember/services/terrain.py` | Multi-layer LANDFIRE queries. Parallel point and bbox reads via ThreadPoolExecutor. Layer discovery and registration. |
| **STACService** | `src/ember/services/stac.py` | Sentinel-2 scene discovery via Element 84 Earth Search. Finds scenes by bbox, date, cloud cover. Returns best scene per MGRS tile for full coverage. |
| **SentinelCOGService** | `src/ember/services/sentinel_cog.py` | Sentinel-2 band reads. Multi-scene stitching, truecolor compositing, spectral index computation (NDVI, NDMI, NBR, NDWI). |
| **Terrain router** | `src/ember/routers/terrain.py` | `GET /terrain` — point and bbox queries for LANDFIRE layers. |
| **Fuel router** | `src/ember/routers/fuel.py` | `GET /fuel` — point queries for FBFM40 fuel model codes. |
| **Scenes router** | `src/ember/routers/scenes.py` | `GET /scenes/search`, `/scenes/{id}/bands`, `/scenes/{id}/index` — scene-aware Sentinel-2 access. |
| **Imagery router** | `src/ember/routers/imagery.py` | `GET /imagery/truecolor-cog`, `/imagery/ndvi-cog`, `/imagery/ndmi-cog` — convenience endpoints that wrap scene search + read into a single call. |

### Data Flow

**LANDFIRE** (static, single file per layer):

```
Request (bbox) → TerrainService → COGService → rio-tiler Reader.part()
    → S3 HTTP range request → numpy array → base64 GeoTIFF response
```

**Sentinel-2** (dynamic, scene discovery required):

```
Request (bbox + dates) → STACService.search_coverage()
    → Earth Search STAC API → best scene per MGRS tile
    → SentinelCOGService.read_bands_mosaic()
    → parallel rio-tiler Reader.part() per scene per band
    → stitch arrays onto single canvas
    → band math (index) or RGB composite (truecolor)
    → base64 GeoTIFF or PNG response
```

### Key Design Decisions

**Scoped GDAL environment for S3 authentication.** LANDFIRE COGs live on a private S3 bucket requiring AWS credentials. Sentinel-2 COGs live on the public `sentinel-cogs` bucket requiring no auth. The LANDFIRE pipeline sets AWS credentials globally at module load (`src/ember/services/cog.py:26-42`). The Sentinel-2 pipeline uses a scoped `rasterio.Env(AWS_NO_SIGN_REQUEST="YES")` context manager (`src/ember/services/sentinel_cog.py:73-88`) that does not affect the LANDFIRE pipeline — `rasterio.Env` uses a thread-local stack.

**Scene-aware vs mosaic.** Copernicus returned opaque mosaics — you couldn't tell which satellite pass contributed. The COG pipeline is scene-aware: every response includes `scene_id`, `datetime`, and `cloud_cover`. When a bbox spans multiple MGRS tiles, `search_coverage()` finds the clearest scene per tile and `read_bands_mosaic()` stitches them into a seamless image.

**Band math in numpy, not server-side.** Copernicus ran evalscripts server-side. The COG pipeline reads raw reflectance bands and computes indices locally — `(A - B) / (A + B)` in numpy. This gives full control over the formula and avoids Copernicus's evalscript abstraction.

**Thread pool for blocking I/O.** rio-tiler's `Reader` is synchronous (GDAL is C-level blocking I/O). Both pipelines use `ThreadPoolExecutor` with `asyncio.run_in_executor()` to avoid blocking the FastAPI event loop. LANDFIRE and Sentinel-2 each use 8-worker pools.

## Implementation Details

### LANDFIRE Pipeline

LANDFIRE data is organized as one continental-scale GeoTIFF per layer, stored on a private S3 bucket. Each file covers CONUS at 30m resolution. The `TerrainService` (`src/ember/services/terrain.py:149`) discovers available layers at startup by matching filenames to known patterns:

```python
# From src/ember/services/terrain.py, LAYER_PATTERNS
LAYER_PATTERNS = {
    "fuel": "F40",       # FBFM40 fuel model
    "slope": "SlpD",     # Slope in degrees
    "aspect": "Asp",     # Aspect in degrees
    "elevation": "Elev", # Elevation in meters
    "canopy_height": "CH",
    "canopy_base_height": "CBH",
    "canopy_bulk_density": "CBD",
    "canopy_cover": "CC",
}
```

**Point queries** go through `COGService.point_query()` (`src/ember/services/cog.py:74`), which opens a `rio-tiler.Reader`, fetches the tile index (~16KB), then fetches only the tile containing the point (~32KB). Coordinate transformation from WGS84 to the raster's CRS is handled by pyproj.

**Bbox raster queries** use `Reader.part()` (`src/ember/services/terrain.py`), which reads the intersection of the bbox with the raster. Categorical layers (fuel) use nearest-neighbor resampling; continuous layers (elevation, slope) use bilinear.

**Full-extent raster queries** (bbox omitted) use `Reader.preview(max_size=OVERVIEW_MAX_SIZE)` (`src/ember/services/terrain.py`), which picks the pyramid overview closest to the target pixel count. LANDFIRE CONUS COGs at 30m native resolution with overview levels `[2, 4, 8, 16, 32, 64, 128]` land on overview 128 at the default 1200px cap, producing a ~1221×793 px image at ~5km/pixel. This assumes LANDFIRE COGs were built with `gdaladdo` at publish time — if overviews are missing, rio-tiler silently falls back to full-resolution resampling, which defeats the purpose.

### Sentinel-2 Pipeline

Sentinel-2 data is organized as individual scene files on the public `sentinel-cogs` S3 bucket. Each scene is one satellite pass over one MGRS tile (~110km square), with a separate COG file per spectral band.

**Scene discovery** uses the Element 84 Earth Search STAC API via `pystac-client` (`src/ember/services/stac.py:147`). A query specifies bbox, date range, and max cloud cover. Earth Search returns STAC items with asset hrefs pointing to S3 COG paths. The `_item_to_scene()` function (`src/ember/services/stac.py:121`) maps Earth Search's common-name asset keys (`red`, `nir`, `swir16`) back to canonical Sentinel-2 band IDs (`B04`, `B08`, `B11`).

**Multi-scene stitching** handles bboxes that span MGRS tile boundaries. `search_coverage()` (`src/ember/services/stac.py:245`) fetches up to 20 scenes, then `pick_best_per_tile()` (`src/ember/services/stac.py:155`) selects the clearest scene per MGRS tile. `read_bands_mosaic()` (`src/ember/services/sentinel_cog.py:204`) reads each band from each scene in parallel and composites them onto a single canvas — pixels from later scenes fill gaps left by earlier ones.

**Band resolution mismatch** is handled during index computation. B08 (NIR) is 10m resolution while B11/B12 (SWIR) are 20m, producing different array shapes for the same bbox. `compute_index()` (`src/ember/services/sentinel_cog.py:372`) resamples the coarser band to match the finer one using `scipy.ndimage.zoom` with bilinear interpolation before performing band math.

**Spectral indices** are computed as normalized differences:

| Index | Formula | Bands | Use |
|-------|---------|-------|-----|
| NDVI | (B08 - B04) / (B08 + B04) | NIR, Red | Vegetation density |
| NDMI | (B08 - B11) / (B08 + B11) | NIR, SWIR | Fuel moisture / fire risk |
| NBR | (B08 - B12) / (B08 + B12) | NIR, SWIR | Burn severity |
| NDWI | (B03 - B08) / (B03 + B08) | Green, NIR | Water bodies |

### Caching

Both pipelines cache results in-memory with TTL-based expiration. Cache keys round coordinates to 3 decimals (~110m) to improve hit rates when the viewport shifts slightly.

| Cache | TTL | Max Entries | Key Includes |
|-------|-----|-------------|--------------|
| Terrain point | 30 min | 1,000 | lat, lon, layers |
| Terrain raster (bbox) | 24 hr | 100 | layer, bbox, max_size |
| Terrain raster (full extent) | 24 hr | 100 | layer, max_size (no bbox) |
| STAC search | 1 hr | 200 | bbox, dates, cloud cover, limit |
| STAC scene | 1 hr | 500 | scene_id |
| Sentinel-2 band read (bbox) | 24 hr | 100 | scene_ids, bands, bbox, max_size, format |
| Sentinel-2 band read (preview) | 24 hr | 100 | scene_id, bands, max_size, format (no bbox) |

LANDFIRE data is static (updated annually), so long TTLs are safe. Sentinel-2 scenes are immutable once published, so 24hr band read caches are safe. STAC search results get 1hr TTL because new scenes are ingested as Sentinel-2 passes occur.

### Configuration

```bash
# LANDFIRE (private S3, credentials required)
LANDFIRE_S3_PREFIX=s3://stellaris-landfire-data/Tif
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-west-2

# Sentinel-2 (public S3, no credentials needed)
EARTH_SEARCH_URL=https://earth-search.aws.element84.com/v1  # default
```

## Endpoint Contracts

FastAPI auto-generates interactive contract docs at `/docs` (Swagger UI) and
`/openapi.json` — those are the source of truth for live schemas. The sections
below codify the prose contract: the bbox-optional semantics, mode-switching
rules, error envelope, and response headers. Anything non-obvious from the
OpenAPI surface alone is documented here.

All endpoints require `Authorization: Bearer <JWT>` in production. Dev mode
(`ENVIRONMENT=development` with no auth settings) auto-authenticates.

All endpoints are mounted under `/api/v1`. Paths below omit that prefix.

### `GET /terrain` — LANDFIRE multi-layer query

Three mutually-exclusive modes selected by query params:

| Mode | Trigger | Returns |
|------|---------|---------|
| Point | `lat`, `lon` present | JSON, one scalar per requested layer |
| Bbox raster | all four bbox params + `format=raster` | Base64 GeoTIFF, bbox-cropped, native resolution |
| Full extent | no point, no bbox, `format=raster` | Base64 GeoTIFF, full CONUS via pyramid overview |

**Request params — common**

| Param | Type | Modes | Description |
|-------|------|-------|-------------|
| `layers` | CSV / single name | all | Layer names. Required (single) in raster modes; optional (default: all) in point mode. |
| `format` | `json` / `raster` | all | Default `json`. `raster` required for bbox/full-extent modes. |
| `max_size` | int [64, 2048] | raster modes | Max pixel dimension. Default 512 for bbox; `OVERVIEW_MAX_SIZE` (1200) for full-extent. |

**Request params — point mode**

| Param | Type | Required |
|-------|------|:--------:|
| `lat` | float [-90, 90] | ✓ |
| `lon` | float [-180, 180] | ✓ |

**Request params — bbox raster mode**

| Param | Type | Required |
|-------|------|:--------:|
| `min_lat` / `max_lat` / `min_lon` / `max_lon` | float | all-or-none (see Bbox Contract) |

Bbox must satisfy `min < max` on each axis; max span is 10° per axis.

**Response — raster modes**

```json
{
  "status": "success",
  "layer": "fuel",
  "bbox": [-125.0, 24.0, -66.0, 50.0],
  "raster": {
    "format": "geotiff",
    "encoding": "base64",
    "data": "<base64>",
    "width": 1221,
    "height": 793
  },
  "stats": { "min": 91.0, "max": 204.0, "mean": 165.0 }
}
```

**Status codes**

| Code | Condition |
|------|-----------|
| 200 | Success |
| 400 | Mixing point+bbox, partial bbox, multi-layer in raster mode, unknown layer, bbox > 10°, invalid coords |
| 502 | Rasterio / S3 read failure |
| 503 | `LANDFIRE_S3_PREFIX` not configured |

**Response headers**

| Header | Condition | Value |
|--------|-----------|-------|
| `Cache-Control` | Full-extent mode only | `public, max-age=86400` (24h) |

**Examples**

```bash
# Point — JSON
curl "localhost:8001/api/v1/terrain?lat=34.05&lon=-118.25"

# Bbox raster — base64 GeoTIFF
curl "localhost:8001/api/v1/terrain?min_lat=34&max_lat=34.5&min_lon=-118.5&max_lon=-118&layers=elevation&format=raster"

# Full CONUS — pyramid overview + Cache-Control: 24h
curl "localhost:8001/api/v1/terrain?format=raster&layers=fuel"
```

---

### `GET /fuel` — LANDFIRE FBFM40 point query

Shorthand for `/terrain?layers=fuel` in point mode. Returns the Scott & Burgan
fuel model code.

| Param | Type | Required |
|-------|------|:--------:|
| `lat` | float [-90, 90] | ✓ |
| `lon` | float [-180, 180] | ✓ |

**Response**
```json
{ "status": "success", "fuel_model": { "code": "SH2", "type": "Shrub" } }
```

**Status codes:** `200`, `400` (invalid coords), `502`, `503`.

---

### `GET /imagery/{ndvi-cog, ndmi-cog, truecolor-cog}` — Sentinel-2 convenience

Three endpoints sharing the same contract shape. They search STAC for the
clearest recent scene(s), read the required bands from public AWS COGs, and
return results in a single call.

**Two modes** selected by bbox presence:

| Mode | Trigger | Scene selection | Stitching |
|------|---------|-----------------|:---------:|
| Full extent | no bbox params | Single most-recent cloud-free scene inside `SENTINEL_DEFAULT_REGION` (default CONUS `-125,24,-66,50`) | No |
| Bbox crop | all four bbox params | Best scene per MGRS tile intersecting the bbox | Yes, if >1 tile |

See [Bbox Contract](#bbox-contract) for partial-bbox handling.

**Request params** (all endpoints)

| Param | Type | Required | Description |
|-------|------|:--------:|-------------|
| `min_lat` / `max_lat` / `min_lon` / `max_lon` | float | all-or-none | Bbox (max 10° per axis). Omitted → full-extent mode. |
| `start_date` / `end_date` | YYYY-MM-DD | — | Scene date range (default: 30 days ago → today) |
| `max_cloud_cover` | float [0, 100] | — | Max scene cloud cover % (default 20) |
| `max_size` | int [64, 2048] | — | Max pixel dim. Default 512 for bbox, `OVERVIEW_MAX_SIZE` (1200) for full-extent. |
| `format` | enum | — | `stats` (default) or `raster` for ndvi/ndmi-cog; `png` (default) or `raster` for truecolor-cog |

**Response — ndvi-cog** (ndmi-cog substitutes `ndmi` + `moisture_status`/`fire_risk`)

```json
{
  "status": "success",
  "scene_id": "S2B_10SEH_20260415_0_L2A",
  "datetime": "2026-04-15T18:30:00Z",
  "cloud_cover": 3.2,
  "scenes_used": 1,
  "date_range": { "start": "2026-03-16", "end": "2026-04-15" },
  "index": "NDVI",
  "bbox": [-122.0, 37.0, -121.0, 38.0],
  "bands_used": ["B08", "B04"],
  "stats": { "min": -0.1, "max": 0.9, "mean": 0.42 },
  "ndvi": { "mean": 0.42, "min": -0.1, "max": 0.9, "vegetation_status": "Healthy Vegetation" },
  "source": "Sentinel-2 L2A (AWS COG)",
  "raster": { "format": "geotiff", "encoding": "base64", "data": "<base64>", "width": 1098, "height": 1098 }
}
```

The `raster` field is omitted when `format=stats`. `scenes_used` is `1` on
full-extent, and `len(scenes)` on bbox (post MGRS-tile dedup).

**Response — truecolor-cog**

```json
{
  "status": "success",
  "scene_id": "S2B_10SEH_20260415_0_L2A",
  "datetime": "2026-04-15T18:30:00Z",
  "cloud_cover": 3.2,
  "scenes_used": 1,
  "bbox": [-122.0, 37.0, -121.0, 38.0],
  "bands": ["B04", "B03", "B02"],
  "raster": { "format": "image/png", "encoding": "base64", "data": "<base64>", "width": 1200, "height": 1200 },
  "source": "Sentinel-2 L2A (AWS COG)"
}
```

**Status codes**

| Code | Condition |
|------|-----------|
| 200 | Success |
| 400 | Invalid `format`, partial bbox, inverted coords, bbox > 10° |
| 404 | No Sentinel-2 scenes match date range / cloud cover / region |
| 502 | STAC API error or COG read failure |

**Response headers**

| Header | Condition | Value |
|--------|-----------|-------|
| `Cache-Control` | Full-extent mode only | `public, max-age=21600` (6h) |

**Examples**

```bash
# Full scene (no bbox) — Cache-Control: 6h
curl "localhost:8001/api/v1/imagery/ndvi-cog"

# Bbox crop (may stitch across MGRS tiles)
curl "localhost:8001/api/v1/imagery/ndvi-cog?min_lat=34&max_lat=34.5&min_lon=-118.5&max_lon=-118"

# True-color as PNG
curl "localhost:8001/api/v1/imagery/truecolor-cog?min_lat=34&max_lat=34.5&min_lon=-118.5&max_lon=-118" \
  -o truecolor.json
```

---

### `GET /scenes/*` — scene-aware endpoints

For callers needing explicit scene-level control (pick a specific scene by
date, read multiple bands individually). These endpoints predate ORQ-140 and
**always require bbox params** — they do not participate in the bbox-optional
contract.

```bash
# Step 1 — find scenes
curl "localhost:8001/api/v1/scenes/search?min_lat=34&max_lat=34.5&min_lon=-118.5&max_lon=-118&start_date=2026-03-01&end_date=2026-04-05&max_cloud_cover=20"

# Step 2 — read a band from a specific scene
curl "localhost:8001/api/v1/scenes/S2A_11SLT_20260315_0_L2A/bands?min_lat=34&max_lat=34.5&min_lon=-118.5&max_lon=-118"
```

Full request/response schemas: `/docs`.

---

### Bbox Contract

All four bbox params (`min_lat`, `max_lat`, `min_lon`, `max_lon`) behave as a
single atomic group on the endpoints in this section:

| Input | Effect |
|-------|--------|
| All four absent | Full-extent mode (terrain + imagery only; scenes/* rejects) |
| All four present | Bbox-crop mode |
| Any partial combination | `400 ValidationError` with the missing param names |

Additional axis-level validation: `min < max` on both axes, each axis span ≤ 10°.

### Error envelope

All 4xx/5xx responses share FastAPI's error shape:

```json
{ "detail": "<human-readable message>" }
```

Examples from this pipeline:

| Status | Detail (abbreviated) |
|--------|----------------------|
| 400 | `Incomplete bbox: missing max_lon. Provide all four (min_lat, max_lat, min_lon, max_lon) or none.` |
| 400 | `min_lat must be less than max_lat` |
| 400 | `Unknown layer: unobtainium. Available: [...]` |
| 404 | `No Sentinel-2 scenes found in default region [...] between ... with cloud cover < 20%` |
| 502 | `COG read error: <rasterio message>` |
| 503 | `Terrain service not configured (LANDFIRE_S3_PREFIX not set)` |

### Pitfalls

- **Bbox too large**: All endpoints reject bboxes exceeding 10 degrees per dimension. This prevents accidental multi-GB reads.
- **Antimeridian**: Bboxes crossing the antimeridian (e.g., 170 to -170 longitude) are rejected. Known limitation.
- **LANDFIRE coverage**: LANDFIRE only covers the US (CONUS, Alaska, Hawaii, Puerto Rico). Queries outside these bounds return `out_of_bounds`.
- **Sentinel-2 cloud cover**: The `max_cloud_cover` filter is scene-level metadata, not pixel-level. A "10% cloud cover" scene may still have clouds over your specific bbox.
- **Missing overviews on LANDFIRE COGs**: The full-extent `/terrain?format=raster` path relies on `gdaladdo` having been run at publish time. If overviews are missing, rio-tiler falls back to full-resolution resampling — a 156k × 101k pixel read — which is very slow. Verify with `rasterio.open(url).overviews(1)` after publishing a new layer.
- **Partial bbox**: If any of `min_lat`/`max_lat`/`min_lon`/`max_lon` is provided, all four must be provided. A partial bbox returns `400` rather than silently defaulting to full extent.

## Testing

Tests are mock-based — no real S3 or STAC API access required.

| Test File | Coverage |
|-----------|----------|
| `tests/test_terrain_bbox.py` | TerrainService bbox validation (parameterized) |
| `tests/test_terrain_raster_caching.py` | Raster cache hit/miss, size management, TTL |
| `tests/test_terrain_router_bbox.py` | Terrain router parameter parsing and routing |
| `tests/test_stac_service.py` | STAC search, caching, scene-to-band mapping, pick_best_per_tile |
| `tests/test_sentinel_cog.py` | Band reads, truecolor compositing, index math, resolution mismatch, multi-scene stitching |
| `tests/test_full_extent_overviews.py` | Bbox-optional contract (ORQ-140): all-or-none validation, no-bbox full-extent preview path, Cache-Control headers |
| `tests/test_scenes_router.py` | All scene and imagery endpoints, auth, validation, 404/400 cases |

Run all COG-related tests:

```bash
pytest tests/test_terrain_bbox.py tests/test_terrain_raster_caching.py \
       tests/test_terrain_router_bbox.py tests/test_stac_service.py \
       tests/test_sentinel_cog.py tests/test_scenes_router.py -v
```

## Known Limitations

- **No pixel-level cloud masking**: Sentinel-2 scenes include an SCL (Scene Classification Layer) band that identifies cloud pixels, but the pipeline does not use it yet. Cloud-heavy areas within an otherwise clear scene will show clouds in the imagery.
- **Single-date per tile**: Multi-scene stitching picks one scene per MGRS tile. Adjacent tiles may come from different dates, causing slight color/lighting differences at tile boundaries.
- **No temporal composites**: The pipeline returns data from individual scenes, not time-averaged composites. For change detection (e.g., pre/post fire NBR), the consumer must make two requests with different date ranges.
- **Zero-pixel nodata**: The mosaic stitching uses pixel value 0 as the nodata sentinel. Sentinel-2 L2A reflectance is scaled 0-10000, so true zero reflectance is effectively impossible — but this is an assumption, not a guarantee. All Sentinel-2 GeoTIFF outputs (truecolor `raster` format and all spectral index outputs) include `nodata=0` in the rasterio profile so renderers like Nova can treat zero-valued pixels as transparent rather than opaque black.

## Shared Technology Stack

```
rio-tiler 7.x    ─── High-level COG reader (part, point, tile methods)
    │
rasterio 1.4.x   ─── Python bindings for GDAL (file I/O, CRS, transforms)
    │
GDAL              ─── C library for raster I/O, HTTP range requests, /vsis3/ virtual filesystem
    │
numpy             ─── Array operations, band math, compositing
scipy             ─── Resampling (ndimage.zoom for band resolution mismatch)
Pillow            ─── PNG encoding for truecolor imagery
pyproj            ─── Coordinate transformation (WGS84 ↔ UTM)
pystac-client     ─── STAC API client (Sentinel-2 scene discovery only)
```
