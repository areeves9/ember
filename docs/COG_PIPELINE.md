# Cloud Optimized GeoTIFF (COG) Pipeline

Ember uses Cloud Optimized GeoTIFFs for efficient raster data queries against LANDFIRE datasets stored in S3.

## Overview

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────────┐
│  Ember API  │ ──► │  rio-tiler   │ ──► │  S3 COG             │
│  /fuel?lat  │     │  point()     │     │  (HTTP Range GET)   │
└─────────────┘     └──────────────┘     └─────────────────────┘
                           │
                           │ ~50KB range request
                           │ (vs 3GB full download)
                           ▼
                    ┌──────────────┐
                    │ Pixel Value  │
                    │ → Fuel Code  │
                    └──────────────┘
```

## What is a COG?

A Cloud Optimized GeoTIFF reorganizes raster data for efficient HTTP range requests:

```
Regular GeoTIFF:
┌─────────────────────────────────────┐
│ Header │ Pixel Data (sequential)    │  ← Must download entire file
└─────────────────────────────────────┘

Cloud Optimized GeoTIFF:
┌────────────────────────────────────────────────────────┐
│ Header + Tile Index (where each tile lives in file)   │
├────────┬────────┬────────┬────────┬────────┬──────────┤
│ Tile 0 │ Tile 1 │ Tile 2 │ Tile 3 │ Tile 4 │ ...      │
│ 256x256│ 256x256│ 256x256│ 256x256│ 256x256│          │
└────────┴────────┴────────┴────────┴────────┴──────────┘
         ↑
         └── HTTP Range request fetches only needed tile
```

## Architecture

### Components

| Component | Purpose |
|-----------|---------|
| `services/cog.py` | COG service with point queries via rio-tiler |
| `services/landfire.py` | Fuel model service (uses COG or REST fallback) |
| S3 Bucket | Stores COG files (`stellaris-landfire-data`) |

### Data Flow

1. Request: `GET /api/v1/fuel?lat=34.05&lon=-118.25`
2. `LandfireService` checks if COG URL is configured
3. `COGService.point_query()` uses rio-tiler to:
   - Fetch tile index from S3 (~16KB)
   - Calculate which tile contains the coordinates
   - Fetch only that tile (~32KB)
   - Extract pixel value
4. Convert pixel value (91-204) to fuel model code (NB1, GR2, SH5, etc.)
5. Return response with fuel type and description

## Configuration

### Environment Variables

```bash
# S3 path to LANDFIRE COG
LANDFIRE_COG_URL=s3://stellaris-landfire-data/Tif/LC24_F40_250.tif

# AWS credentials for S3 access
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-west-2
```

### Fallback Behavior

If `LANDFIRE_COG_URL` is not set, the service falls back to the LANDFIRE REST API:
- Slower (~500ms vs ~100ms)
- Rate limited
- Dependent on external service availability

## Adding LANDFIRE Layers to S3

LANDFIRE GeoTIFFs are already internally tiled and work directly with rio-tiler - no COG conversion needed.

### Steps

1. **Download from LANDFIRE**
   - Go to https://landfire.gov/data/FullExtentDownloads
   - Select layer (FBFM40, Slope, Aspect, etc.)
   - Download CONUS ZIP

2. **Extract the TIF**
   ```bash
   unzip LF2024_FBFM40_250_CONUS.zip -d landfire_fbfm40
   # TIF is in: landfire_fbfm40/Tif/LC24_F40_250.tif
   ```

3. **Upload to S3**
   ```bash
   aws s3 cp landfire_fbfm40/Tif/*.tif s3://stellaris-landfire-data/Tif/ --profile stellaris
   ```

4. **Configure Ember**
   ```bash
   LANDFIRE_COG_URL=s3://stellaris-landfire-data/Tif/LC24_F40_250.tif
   ```

### Available Layers

| Layer | File Pattern | Size | Env Var |
|-------|--------------|------|---------|
| FBFM40 (Fuel) | LC24_F40_*.tif | 3.1 GB | `LANDFIRE_FBFM40_URL` |
| Slope | LC20_SlpD_*.tif | 4.9 GB | `LANDFIRE_SLOPE_URL` |
| Aspect | LC20_Asp_*.tif | 8.5 GB | `LANDFIRE_ASPECT_URL` |
| Canopy Height | LC24_CH_*.tif | 1.8 GB | `LANDFIRE_CH_URL` |
| Canopy Base Height | LC24_CBH_*.tif | 2.2 GB | `LANDFIRE_CBH_URL` |
| Canopy Bulk Density | LC24_CBD_*.tif | 1.8 GB | `LANDFIRE_CBD_URL` |
| Canopy Cover | LC24_CC_*.tif | 1.8 GB | `LANDFIRE_CC_URL` |
| Elevation | LC20_Elev_*.tif | 8.4 GB | `LANDFIRE_ELEV_URL` |

## FBFM40 Fuel Model Codes

LANDFIRE FBFM40 pixel values map to Scott & Burgan fuel model codes:

| Pixel Range | Code Prefix | Fuel Type |
|-------------|-------------|-----------|
| 91-99 | NB | Non-burnable (urban, water, bare ground) |
| 101-109 | GR | Grass |
| 121-124 | GS | Grass-Shrub |
| 141-149 | SH | Shrub |
| 161-165 | TU | Timber-Understory |
| 181-189 | TL | Timber Litter |
| 201-204 | SB | Slash-Blowdown |

## Performance

| Method | Data Transfer | Latency |
|--------|--------------|---------|
| Full GeoTIFF download | 3 GB | Minutes |
| COG point query | ~50 KB | ~100-200ms |
| REST API fallback | N/A | ~500ms+ |

## Extending for Fire Prediction

Additional LANDFIRE layers can be added for fire behavior modeling:

| Layer | Use Case | Download |
|-------|----------|----------|
| FBFM40 | Fuel model (current) | ✅ |
| SLOPE | Terrain slope affects spread rate | landfire.gov |
| ASPECT | Wind direction interaction | landfire.gov |
| CBD | Canopy bulk density (crown fire) | landfire.gov |
| CBH | Canopy base height (crown fire initiation) | landfire.gov |
| CC | Canopy cover % | landfire.gov |

To add a new layer:
1. Download from LANDFIRE
2. Convert to COG
3. Upload to S3
4. Add new config variable (e.g., `LANDFIRE_SLOPE_COG_URL`)
5. Create service method to query it

## Troubleshooting

### "Query failed: Access Denied"
- Check AWS credentials are set correctly
- Verify IAM user has S3 read access to the bucket

### "Coordinates outside raster extent"
- LANDFIRE only covers US (CONUS, Alaska, Hawaii, Puerto Rico)
- Verify coordinates are within bounds

### Slow queries
- Ensure file is properly tiled (COG format)
- Check S3 bucket is in same region as Ember deployment
- Verify GDAL environment variables are set (see `cog.py`)
