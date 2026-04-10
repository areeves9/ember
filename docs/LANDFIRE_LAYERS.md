# LANDFIRE Layer Catalog

Ember hosts LANDFIRE raster layers as Cloud Optimized GeoTIFFs on S3 (`stellaris-landfire-data`). This document describes every layer we include, why we include it, and which available LANDFIRE products we intentionally omit.

All layers cover CONUS at 30m resolution. Storage cost is ~$2/month for the full suite.

## Included Layers (20)

### Topographic (LF 2020)

Static terrain data. These don't change between LANDFIRE releases.

| Layer | Code | Pattern | Unit | Description |
|-------|------|---------|------|-------------|
| Slope Degrees | `slope` | `SlpD` | Degrees (0-90) | Terrain slope. Steeper slopes accelerate fire spread uphill. |
| Aspect | `aspect` | `Asp` | Degrees (0-360), -1=flat | Direction a slope faces. South-facing slopes are drier and more fire-prone in the Northern Hemisphere. |
| Elevation | `elevation` | `Elev` | Meters | Elevation above sea level. Influences vegetation type, moisture, and fire behavior. |

### Canopy (LF 2024)

Forest canopy structure. Updated with each LANDFIRE release as disturbance and regrowth alter canopy characteristics.

| Layer | Code | Pattern | Unit | Description |
|-------|------|---------|------|-------------|
| Canopy Height | `canopy_height` | `CH` | Meters (stored as m x 10) | Height of the forest canopy. Taller canopy = greater potential for crown fire. |
| Canopy Base Height | `canopy_base_height` | `CBH` | Meters (stored as m x 10) | Height from ground to the bottom of the canopy. Low CBH means surface fire can transition to crown fire more easily. |
| Canopy Bulk Density | `canopy_bulk_density` | `CBD` | kg/m3 (stored as kg/m3 x 100) | Mass of canopy fuel per unit volume. Higher density sustains active crown fire. |
| Canopy Cover | `canopy_cover` | `CC` | Percent (0-100) | Percentage of ground covered by tree canopy. Affects wind reduction and fuel moisture. |

### Fuel (LF 2024)

Fire behavior fuel classifications. Two systems are included because they serve different modeling frameworks.

| Layer | Code | Pattern | Unit | Description |
|-------|------|---------|------|-------------|
| FBFM40 | `fuel` | `F40` | Categorical (91-204) | Scott & Burgan 40 fuel model. Primary fuel classification used by modern fire behavior tools (BehavePlus, FlamMap). Maps to grass, shrub, timber, and slash categories. |
| FBFM13 | `fuel_model_13` | `FBFM13` | Categorical (1-13) | Anderson 13 fuel model. Legacy classification still used by many operational fire management tools and historical comparisons. |

### Vegetation (LF 2024 / LF 2020)

Ecological vegetation data. More descriptive than fuel models — these tell you *what's growing*, not just how it burns.

| Layer | Code | Pattern | Version | Unit | Description |
|-------|------|---------|---------|------|-------------|
| Existing Vegetation Type | `vegetation_type` | `EVT` | 2024 | Categorical | Specific plant community (e.g., "Rocky Mountain Mesic Montane Mixed Conifer"). More descriptive than fuel codes for user-facing context. Key feature for classification model. |
| Existing Vegetation Cover | `vegetation_cover` | `EVC` | 2024 | Percent | Canopy cover broken down by vegetation type. More granular than generic canopy cover (CC). |
| Existing Vegetation Height | `vegetation_height` | `EVH` | 2024 | Meters | Vegetation height by type. Complements canopy height with type-specific measurements. |
| Biophysical Settings | `biophysical_settings` | `BPS` | 2020 | Categorical | Pre-European settlement vegetation. The historical baseline — comparing current vegetation to BPS reveals how much an area has changed from its natural state. |

### Fire Regime (LF 2016 / LF 2024)

Historical and current fire regime characterization. These layers answer: *Is fire normal here? How has the landscape changed?*

The 2016 layers represent the most recent LANDFIRE release of these products. Fire regime characteristics are derived from long-term ecological models that don't change with each release cycle.

| Layer | Code | Pattern | Version | Unit | Description |
|-------|------|---------|---------|------|-------------|
| Fire Regime Groups | `fire_regime_group` | `FRG` | 2016 | Categorical (1-5) | Historical fire frequency and severity class. Answers "is fire expected here?" — from frequent low-severity (Group I, e.g., pine savannas) to very rare replacement (Group V, e.g., alpine). |
| Fire Return Interval | `fire_return_interval` | `FRI` | 2016 | Years | Mean number of years between fires historically. Complements FRG with the actual interval — FRG says "frequent", FRI says "every 7 years". |
| Percent Fire Severity | `percent_fire_severity` | `PFS` | 2016 | Percent | Proportion of historical fires that were high-severity (stand-replacing). Completes the fire regime trifecta: FRG (type), FRI (frequency), PFS (severity). |
| Vegetation Departure | `vegetation_departure` | `VDep` | 2024 | Percent (0-100) | How far current vegetation has departed from its historical range of variability. High departure signals altered fire risk — the landscape no longer behaves the way its fire regime predicts. |
| Vegetation Condition Class | `vegetation_condition` | `VCC` | 2024 | Categorical (1-3) | Classified version of departure: 1 = within historical range, 2 = moderately departed, 3 = significantly departed. Useful for quick triage. |
| Succession Classes | `succession_classes` | `SClass` | 2024 | Categorical | Current vegetation succession state relative to reference conditions. Early succession burns differently than late succession — early has more grass/shrub (fast surface fire), late has more canopy fuel (crown fire potential). |

### Disturbance (LF 2024)

Recent landscape disturbance that alters fuel loading and fire behavior.

| Layer | Code | Pattern | Unit | Description |
|-------|------|---------|------|-------------|
| Fuel Disturbance | `fuel_disturbance` | `FDist` | Categorical | Recent disturbance events (fire, harvest, insects, disease) that change fuel characteristics. A recently burned area has fundamentally different fire behavior than surrounding undisturbed fuels. |

## Omitted Products

The following LANDFIRE CONUS products are available but intentionally excluded.

### Redundant with included layers

| Product | Code | Why omitted |
|---------|------|-------------|
| Slope Percent Rise | `SlpP` | Redundant with Slope Degrees (`SlpD`). Same data, different unit. Degrees is the standard in fire behavior modeling. |
| Fuel Vegetation Type | `FVT` | Simplified derivative of EVT, reformatted for fire modeling tools (FARSITE, FlamMap). Ember serves EVT directly, which is more descriptive. Only useful if feeding fire spread simulation software. |
| Fuel Vegetation Cover | `FVC` | Simplified derivative of EVC for fire modeling tools. Same reasoning as FVT. |
| Fuel Vegetation Height | `FVH` | Simplified derivative of EVH for fire modeling tools. Same reasoning as FVT. |

### Too specialized or outdated

| Product | Code | Why omitted |
|---------|------|-------------|
| FCCS Fuelbeds | `FCCS` | Fuel Characteristic Classification System. Highly specialized fuelbed descriptions used in CONSUME/FOFEM smoke and emissions modeling. Not useful for fire risk assessment or user-facing context. |
| Environmental Site Potential | `ESP` | Vegetation that *could* grow at a site based on climate and soils. Only available as LF 2014. Niche use case — BPS serves a similar historical baseline role with more recent data. |
| National Vegetation Classification | `NVC` | Older vegetation classification system (LF 2016). EVT is more current (LF 2024) and more widely used. |

### Not single-layer rasters

| Product | Code | Why omitted |
|---------|------|-------------|
| Annual Disturbance 1999-Present | `Dist` | Multi-year time series archive, not a single raster layer. Would require fundamentally different handling (temporal queries, year selection). The `FDist` layer captures the current disturbance state. |
| Limited Annual Disturbance | `LDist` | Preliminary/limited disturbance detection. Subset of the annual disturbance time series. Same architectural mismatch. |
| Preliminary Annual Disturbance | `PDist` | Pre-release disturbance data. Same as above. |
| Operational Roads | `Roads` | Vector data (road centerlines), not raster. Ember's COG pipeline reads rasters. Would require a different service and data format. |
| LF Reference Database | `LFRDB` | Tabular reference database, not a raster product. Used for plot-level validation, not spatial queries. |
